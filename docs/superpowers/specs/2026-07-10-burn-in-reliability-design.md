# Burn-In Reliability Control Plane

**Date:** 2026-07-10
**Status:** Approved
**Scope:** Operator-facing health checks for the paper-trading burn-in loop.

## Goal

Give the operator one command that answers "Is the burn-in healthy right now?" and one shell hook that runs the same checks automatically. The recent 2026-07-09 incident (burn-in hung 7+ hours at the scan step with a stale-but-alive PID, EOD exit missed, dashboard orphan) showed that aliveness is not the same as progress. This design closes that gap without changing trading behavior.

## Non-Goals

- No new trading logic, signal changes, or risk-knob changes.
- No new dependencies.
- No changes to `fetch_bars`, supermodel, or counter-thesis paths.
- No live-trading implications (paper-only by default remains enforced).

## Architecture

Two layers, both narrow:

1. **Heartbeat** — `auto-burn-in.sh` writes a tiny JSON file each loop iteration; same pattern used for `eod_watchdog` markers and per-day download markers.
2. **Health module** — pure functions in `trading_bot/health/` that read local artifacts (no network except a 1-second `127.0.0.1` dashboard probe) and return a structured report. A single CLI surfaces the report and a shell function in `auto-burn-in.sh` records it.

## Components

| Path | Purpose |
|---|---|
| `state/burn_in/heartbeat.json` | One record per loop cycle: `{ts, cycle, last_action, fills, exits, rejects}` |
| `state/burn_in/dashboard.pid` | Dashboard PID file (already exists) |
| `state/burn_in/eod_watchdog.pid` | EOD watchdog PID file (new — tracked the same way) |
| `trading_bot/health/checks.py` | Pure checks, one function per concern |
| `trading_bot/health/runner.py` | Runs all checks, returns `HealthReport` with worst-severity exit code |
| `trading_bot/health/__init__.py` | Public exports |
| `trading_bot/cli/app.py` | Extend existing `doctor` command with `--burn-in` flag |
| `scripts/auto-burn-in.sh` | New `run_health_check()` mirroring `run_nightly_tuning`; runs on startup, every 30 min, and pre-EOD |
| `tests/test_health_checks.py` | One test per check, `tmp_path` fixtures |
| `tests/test_health_runner.py` | Integration tests over synthetic states |
| `tests/test_doctor_burn_in.py` | CLI smoke test: exit codes + `--json` shape |

## Check Definitions

Each check returns `CheckResult(name: str, status: Literal["PASS","WARN","FAIL"], detail: str, observed: dict | None)`.

1. **`check_pid_alive(pid_file: Path)`** — Read PID file; `os.kill(pid, 0)` to test. PASS if alive, FAIL if dead, FAIL if file missing. Severity source: the on-disk PID file is the burn-in's identity.
2. **`check_heartbeat_fresh(heartbeat_path: Path, max_age_seconds: int)`** — Read last `ts`; compare to wall clock. PASS if ≤ 90s, WARN if ≤ 5min, FAIL otherwise. This is the "main loop is making progress" signal.
3. **`check_dashboard_health(port: int)`** — `GET http://127.0.0.1:<port>/api/health` with 1s timeout. PASS if 200, WARN if non-200, FAIL if connection refused. Mirrors the orphan-detection fix in `tests/test_dashboard_orphan_cleanup.py`.
4. **`check_eod_watchdog(pid_file: Path, now_et)`** — PID alive AND scheduled window (today 15:50–16:05 ET or always-on outside market). PASS on weekday before 15:50, PASS if PID alive during 15:50–16:05, WARN/FAIL outside expected windows.
5. **`check_open_positions_consistent(db_path: Path, heartbeat_path: Path)`** — Read `trades` table for `status='FILLED'` count; compare against heartbeat's reported open count. Mismatch = WARN; mismatch AND stale heartbeat = FAIL.
6. **`check_market_data_freshness(db_path: Path, now_et)`** — Read most recent `market_data.timestamp`; compare to market hours. WARN if > 10min stale during market hours, FAIL if > 30min.

## CLI Surface

```bash
./tradebot-local doctor                 # existing health checks (unchanged)
./tradebot-local doctor --burn-in       # full burn-in reliability report
./tradebot-local doctor --burn-in --json
```

Output shape (human, color):

```
[burn-in] PID 13773 alive                     PASS
[burn-in] loop heartbeat fresh (last 38s ago) PASS
[burn-in] dashboard :8080 health 200           PASS
[burn-in] eod watchdog scheduled for 15:55 ET  PASS
[burn-in] no orphan open positions             PASS
[burn-in] market data freshness OK             PASS
Summary: 6 PASS, 0 WARN, 0 FAIL
```

Exit codes (scriptable from `auto-burn-in.sh`):
- `0` PASS (all checks green)
- `1` WARN (one or more warnings, no failures)
- `2` FAIL (one or more failures)

## Shell Integration

New `run_health_check()` in `scripts/auto-burn-in.sh`. Pattern mirrors `run_nightly_tuning()`: capture stdout to `$LOG_DIR/health.log`, never `exit 1` on failure, return 0.

Invoked:
- Once on startup (right after `ensure_dashboard`).
- Every 30 minutes during the loop (add a single line at the bottom of the main while-true).
- Once at 15:50 ET (5 minutes before EOD), as a final pre-close sanity check.

Output appended to `logs/burn_in/health.jsonl` (one JSON object per check run).

## Data Flow

```
auto-burn-in.sh loop iteration
  ↓ writes
state/burn_in/heartbeat.json (each cycle)
  ↓ read by
trading_bot/health/runner.py
  ↓ composes
trading_bot/health/checks.py (pure functions)
  ↓ rendered by
CLI: --json | human    AND    shell: append to logs/burn_in/health.jsonl
```

All reads are local files or a 1s `127.0.0.1` probe. No external network.

## Error Handling

- Each check is isolated — exceptions degrade to `FAIL` with reason text, never abort the report.
- `runner.run_health_checks()` returns a `HealthReport` object with the worst-severity `status` and per-check `CheckResult` list; CLI computes the exit code from this.
- Heartbeat-write failures in `auto-burn-in.sh` are logged but do NOT abort the loop — the heartbeat check will then correctly FAIL, which is the intended signal.

## Testing

- **Per-check unit tests** (`tests/test_health_checks.py`) using `tmp_path` for PID files and heartbeat files. Mock `urllib.request.urlopen` for the dashboard probe.
- **`runner` integration tests** (`tests/test_health_runner.py`) — three cases: all-pass, one-warn, all-fail. Assert exit code mapping is correct.
- **CLI smoke test** (`tests/test_doctor_burn_in.py`) using Typer's `CliRunner`. Assert human output contains all 6 check names and `--json` returns a parseable JSON list.
- **Shell integration test** — extend `tests/test_auto_burn_in_script.py` to assert `run_health_check()` is invoked on startup and at the 30-minute cadence.

Network-free (monkeypatch all `urlopen` calls; use `tmp_path` for `state/burn_in/`).

## Rollout

1. Land the spec and implementation in the same PR.
2. Update `AGENTS.md` to reference `./tradebot-local doctor --burn-in` alongside the existing `kill-switch --status` operator commands.
3. Run one full check on the current live burn-in process before market open to confirm baseline = PASS.

## Open Questions

None. All design decisions captured above.

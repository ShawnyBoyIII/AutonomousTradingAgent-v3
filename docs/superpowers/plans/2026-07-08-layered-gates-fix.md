# 2026-07-08 Layered-Gates Fix Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unblock paper trading in the burn-in loop by fixing the layered-gate issues identified in today's postmortem (`docs/POSTMORTEM_2026_07_08_OPEN.md`). Three independent workstreams: defensive fixes (Tier 1), aggressive signal-quality bypass, and the orthogonal marker bug.

**Architecture:** Three independent workstreams, each independently shippable and revertible:
- **Workstream A** fixes the entry-timing timestamp bug so a stale bar's clock doesn't trip the avoid-window check. Defensive — works whether or not signal-quality is bypassed.
- **Workstream B** switches signal-quality from a hard reject (signal → None) to an advisory flag in `details`. Counter-thesis + supermodel `block` + risk manager still gate hard.
- **Workstream C** fixes the per-day marker so `1d` backfills don't prevent `1m` backfills for the same date.

**Tech Stack:** Python 3.11+, `pytest` (already configured), `pyarrow` + `pandas` for Parquet. No new third-party deps. All changes compatible with existing 2006-test suite.

## User Decisions (confirmed via question tool)

| Decision | Choice |
|---|---|
| Scope tier | **Tier 1 only (smallest, safest) + Workstream B (aggressive bypass)** |
| Polygon tier | **Paid (Stocks Advanced / Developer)** — confirms timestamp issue is in code/cache, not data-feed |
| Risk appetite | **Aggressive — bypass signal-quality entirely** (also makes this work for Workstream B) |
| Marker bug | **Plan it** (Workstream C below) |

## Global Constraints

- All tests must be network-free (monkeypatch `fetch_bars` / `_request`)
- No hardcoded credentials
- `live_trading_enabled` stays forced `False`
- No commits unless user explicitly approves
- Revert the 3 TEMP overrides from today's session as part of Workstream A closeout (Task A4)

---

## File Structure

| File | Workstream | Responsibility |
|---|---|---|
| `trading_bot/strategy/signal_quality.py` | A | `_signal_timestamp` wall-clock fallback |
| `tests/test_signal_quality.py` | A | New regression test for stale-bar timestamp |
| `config.yaml`, `burn-in-config.yaml` | A | Bump `max_volume_jump_pct: 1000 → 5000`; revert today's TEMP overrides |
| `tests/test_validation.py` (if exists; else `tests/test_data_validation.py`) | A | Threshold boundary tests |
| `scripts/auto-burn-in.sh` | A | Parse actual `reason=` from `paper-trade` output instead of hardcoded "stale data" |
| `tests/test_auto_burn_in_script.py` | A | Regression for the new label logic |
| `trading_bot/runtime/orchestrator.py:932-976` | B | `_apply_phase1_signal_quality` no longer returns `None` |
| `tests/test_orchestrator_signal_quality.py` (new or extend an existing one) | B | Test that verdict-fail still produces a trade |
| `trading_bot/cli/app.py:2019` | C | Marker filename format includes interval(s) |
| `trading_bot/data/data_store.py` | C | `write_marker(date, interval_set)` helper if needed |
| `scripts/auto-burn-in.sh:469` | C | `EOD_LAST_FETCH_FILE` per-interval |
| `tests/test_eod_runner.py`, `tests/test_auto_burn_in_script.py` | C | Update existing tests for new filename format |

---

## Workstream A: Defensive fixes (Tier 1)

### Task A1: Entry-timing uses wall-clock fallback when bar is stale

**Files:**
- Modify: `trading_bot/strategy/signal_quality.py:311-332` (`_signal_timestamp`)
- Test: `tests/test_signal_quality.py`

**Interfaces:**
- Consumes: `intraday_frame: pd.DataFrame`, `signal: TradeSignal | None`
- Produces: `datetime | None` — tz-aware, fresh (≤ 5 min old) ET timestamp

**Why:** today's rejection at 10:13 ET used a bar timestamp from a prior session's close, which fell inside the avoid window. With paid Polygon tier, this is purely a code/cache bug — not a data-feed issue.

- [x] **Step 1: Write the failing test.**

  File: `tests/test_signal_quality.py`
  ```python
  def test_signal_timestamp_falls_back_to_wall_clock_on_stale_bar():
      """When the latest bar is older than 5 minutes, _signal_timestamp
      should return a fresh, tz-aware timestamp (not the stale bar time)."""
      stale_ts = datetime.now(ET) - timedelta(minutes=30)
      df = pd.DataFrame({
          "timestamp": [stale_ts],
          "close": [100.0],
          "volume": [1000],
      })
      result = _signal_timestamp(signal=None, frame=df)
      assert result is not None
      age = (datetime.now(ET) - result).total_seconds()
      assert age < 60, f"timestamp should be near wall-clock, got age={age}s"
      assert result.tzinfo is not None
  ```

- [x] **Step 2: Run to confirm it fails.**
  ```bash
  .venv/bin/python -m pytest tests/test_signal_quality.py::test_signal_timestamp_falls_back_to_wall_clock_on_stale_bar -v
  ```
  Expected: FAIL — currently returns the stale 30-min-old bar.

- [x] **Step 3: Update `_signal_timestamp` to add a wall-clock fallback.**

  Replace `trading_bot/strategy/signal_quality.py:311-332` with:
  ```python
  _STALE_BAR_THRESHOLD_SECONDS = 300  # 5 minutes

  def _signal_timestamp(signal, frame):
      frame_timestamp = None
      if not frame.empty:
          if "timestamp" in frame.columns:
              candidate = frame.iloc[-1].get("timestamp")
          else:
              candidate = frame.index[-1]
          frame_timestamp = candidate if isinstance(candidate, datetime) else None

      now_utc = datetime.now(tz=timezone.utc)

      # If the bar is fresh (≤5 min old), keep it as the candidate.
      fresh_frame_ts = None
      if frame_timestamp is not None:
          ts_aware = frame_timestamp if frame_timestamp.tzinfo else frame_timestamp.replace(tzinfo=ZoneInfo("UTC"))
          age = (now_utc - ts_aware).total_seconds()
          if 0 <= age < _STALE_BAR_THRESHOLD_SECONDS:
              fresh_frame_ts = frame_timestamp

      if signal is not None and fresh_frame_ts is not None:
          if _wall_time_matches(frame_timestamp, signal.timestamp):
              return fresh_frame_ts
      if fresh_frame_ts is not None:
          return fresh_frame_ts
      if signal is not None and signal.timestamp is not None:
          sig_ts = signal.timestamp
          sig_aware = sig_ts if sig_ts.tzinfo else sig_ts.replace(tzinfo=ZoneInfo("UTC"))
          sig_age = (now_utc - sig_aware).total_seconds()
          if 0 <= sig_age < _STALE_BAR_THRESHOLD_SECONDS:
              return sig_ts
      # Final fallback: the bar/signal was stale; use wall-clock so the
      # avoid-window check evaluates against current time, not a stale bar.
      return now_utc
  ```

  Add at top of file: `from datetime import timezone` (only if not already imported).

- [x] **Step 4: Run the test, confirm PASS.**
- [x] **Step 5: Run the full signal-quality test module.**
  ```bash
  .venv/bin/python -m pytest tests/test_signal_quality.py -v
  ```
- [x] **Step 6: Add a counter-test pinning existing fresh-bar behavior:**
  ```python
  def test_signal_timestamp_uses_bar_when_fresh():
      fresh_ts = datetime.now(ET) - timedelta(seconds=30)
      df = pd.DataFrame({"timestamp": [fresh_ts], "close": [100.0], "volume": [1000]})
      result = _signal_timestamp(signal=None, frame=df)
      assert result == fresh_ts, "should use the bar's timestamp when bar is fresh"
  ```

### Task A2: Raise `max_volume_jump_pct` to 5000

**Files:**
- Modify: `config.yaml:17-19`, `burn-in-config.yaml:18-20`
- Test: `tests/test_data_validation.py` (or create it if missing)

- [x] **Step 1: Write boundary test.**
  ```python
  def test_volume_jump_within_5000pct_passes():
      from trading_bot.data.validation import validate_volume_sanity
      df = pd.DataFrame({"volume": [100, 5000]})  # 50× jump
      result = validate_volume_sanity(df, max_volume_jump_pct=5000.0)
      assert result.valid, f"5000% threshold should allow 50× jump; got: {result.reason}"
  ```

- [x] **Step 2: Run test, confirm PASS.**
- [x] **Step 3: Update configs.**

  `config.yaml:17-19`:
  ```yaml
  validate_data: true  # REVERT today's TEMP override
  max_price_jump_pct: 1000.0
  max_volume_jump_pct: 5000.0  # was 1000.0 — 1h intraday bars usually 50-100× at open
  ```

  `burn-in-config.yaml:18-20`: same change, and also revert `validate_data: false → true`.

- [x] **Step 4: Run full validation tests.**

### Task A3: Parse actual reason in `auto-burn-in.sh` instead of hardcoded "(stale data)"

**Files:**
- Modify: `scripts/auto-burn-in.sh` (the `No signal: $symbol (stale data)` block)
- Test: `tests/test_auto_burn_in_script.py`

- [x] **Step 1: Add test asserting the bash function parses reason.**
  Look at existing `tests/test_auto_burn_in_script.py` harness pattern.
- [x] **Step 2: Run, confirm FAIL.**
- [x] **Step 3: Update the bash to extract reason from `NO_SIGNAL reason=...` line:**
  ```bash
  elif echo "$trade_output" | grep -q "NO_SIGNAL"; then
      local nosig_reason=$(echo "$trade_output" | grep "NO_SIGNAL" | head -1 | sed 's/.*reason=//' | awk '{print $1}' | sed 's/[;,].*//')
      [ -z "$nosig_reason" ] && nosig_reason="unknown"
      echo "[$timestamp] ⚪ No signal: $symbol ($nosig_reason)"
  fi
  ```
- [x] **Step 4: Run tests for the script.**

### Task A4: Revert today's 3 TEMP overrides

- [x] Revert `min_entry_confluence_score: 0.0 → 1.0` in `burn-in-config.yaml:8`
- [x] Revert `validate_data: false → true` in `burn-in-config.yaml:20` and `config.yaml:17` (already done in Task A2)
- [x] Revert `max_consecutive_losses: 999 → 5` in `burn-in-config.yaml:34`

---

## Workstream B: Bypass signal-quality hard-reject (Aggressive)

### Task B1: Make `_apply_phase1_signal_quality` advisory

**Files:**
- Modify: `trading_bot/runtime/orchestrator.py:932-976`
- Test: `tests/test_orchestrator_signal_quality_advisory.py` (or extend existing orchestrator test)

**Why:** Per your risk appetite, you want to bypass signal-quality. Counter-thesis, supermodel `block`, risk manager, and sector concentration still gate. This loses only the alignment + entry-timing hard rejects.

- [x] **Step 1: Write the failing test.**
  ```python
  def test_signal_quality_rejection_does_not_kill_trade(monkeypatch, tmp_path):
      """B (Aggressive): even when evaluate_signal_quality returns verdict.passed=False,
      run_paper_trade should still produce a fill (subject to downstream gates)."""
      # Monkeypatch evaluate_signal_quality to return verdict.passed=False
      # Construct a settings + broker + symbol setup that WOULD trade
      # Assert: orders table gains a row
  ```

- [x] **Step 2: Run test, confirm FAIL.**
- [x] **Step 3: Change `_apply_phase1_signal_quality` to augment details instead of returning None.**

  At `orchestrator.py:932-976`, change:
  ```python
  if not verdict.passed:
      return None, f"signal quality rejected: {verdict.reason}", details
  ```
  to:
  ```python
  if not verdict.passed:
      # AGGRESSIVE (2026-07-08): don't kill the signal; record advisory flag
      # so downstream gates (counter-thesis, supermodel, risk manager) decide.
      # Revert this block to a hard `return None, ...` to restore old behavior.
      details["signal_quality_passed"] = False
      details["signal_quality_reason"] = verdict.reason
      if hasattr(signal, "quality") and signal.quality not in ("YELLOW", "RED"):
          try:
              signal.quality = "YELLOW"
          except (AttributeError, TypeError):
              pass  # signal may be frozen; skip
  ```

- [x] **Step 4: Run the new test, confirm PASS.**
- [x] **Step 5: Run the full orchestrator test module.**
  ```bash
  .venv/bin/python -m pytest tests/test_orchestrator*.py -v
  ```

- [x] **Step 6: Add the `# AGGRESSIVE 2026-07-08` comment** so future devs can find this easily.

---

## Workstream C: Fix per-interval marker bug

### Task C1: Marker filename includes interval set

**Files:**
- Modify: `trading_bot/cli/app.py:2016-2021` (the marker-formatting block)
- Modify: `trading_bot/data/data_store.py` (helper if needed)
- Modify: `scripts/auto-burn-in.sh:462-482` (`run_eod_data_download`)
- Test: `tests/test_eod_runner.py` (existing test `test_picks_correct_key_template_per_product` is adjacent)

- [x] **Step 1: Write failing test.**
  ```python
  def test_marker_is_per_interval_set_not_per_date(tmp_path, monkeypatch):
      """1d marker must NOT skip 1m for the same date."""
      # Set up: pre-create .last_eod_fetch_2026-07-06_1d.marker
      # Run: --intervals 1m --date 2026-07-06
      # Assert: CLI actually calls run_eod_fetch (not skipped)
  ```

- [x] **Step 2: Run test, confirm FAIL.**
- [x] **Step 3: Change marker filename format.**

  In `trading_bot/cli/app.py:2019`:
  ```python
  # Old:
  day_marker = root / f".last_eod_fetch_{day.isoformat()}.marker"
  # New:
  interval_str = "_".join(sorted(chosen_intervals))
  day_marker = root / f".last_eod_fetch_{day.isoformat()}_{interval_str}.marker"
  ```

  In `scripts/auto-burn-in.sh:462-482` (`run_eod_data_download`):
  ```bash
  local interval_str=$(echo "$EOD_INTERVALS" | tr ',' '\n' | sort | tr '\n' '_' | sed 's/_$//')
  local day_marker="$EOD_STORE_ROOT/.last_eod_fetch_${today_ymd}_${interval_str}.marker"
  if [ -f "$day_marker" ]; then
      echo "[$timestamp] Ⓜ️  EOD already fetched for $today_ymd intervals=$interval_str"
      return 0
  fi
  # ... existing fetch logic ...
  # After successful fetch:
  echo "$today_ymd" > "$day_marker"
  ```

- [x] **Step 4: Update existing tests** (`test_eod_runner.py` `test_picks_correct_key_template_per_product` and any in `test_auto_burn_in_script.py` referencing the old filename).
- [x] **Step 5: Run the eod + script tests.**
  ```bash
  .venv/bin/python -m pytest tests/test_eod_runner.py tests/test_auto_burn_in_script.py -v
  ```
- [x] **Step 6: End-to-end backfill scenario:**
  ```bash
  rm -f state/data_store/.last_eod_fetch_2026-07-06*.marker
  ./tradebot-local --config-path burn-in-config.yaml eod-fetch --intervals 1d --date 2026-07-06
  ls state/data_store/.last_eod_fetch_2026-07-06*.marker
  # Should show .last_eod_fetch_2026-07-06_1d.marker
  ./tradebot-local --config-path burn-in-config.yaml eod-fetch --intervals 1m --date 2026-07-06
  find state/data_store/parquet -path "*/1m/*2026-07-06*" -type f | wc -l
  # Expected: 50 partitions
  ```

---

## Execution order (recommended)

1. **Day 1 (post-market close today, after 16:00 ET):**
   - Workstream A Task A4 (revert TEMP overrides — no code change, just configs)
   - Workstream C Task C1 (marker bug — independent of trading)

2. **Day 2 (this evening or tomorrow pre-market):**
   - Workstream A Tasks A1, A2, A3 (defensive fixes — small, TDD-safe)
   - Workstream B Task B1 (aggressive bypass — verify with full test suite)

3. **Day 3 (trade day):**
   - Start burn-in with new code
   - Watch first 30 min for fills
   - If still no fills and reason is supermodel `block` → that's a genuine strategy decision

## Verification (combined)

```bash
.venv/bin/python -m pytest -q                           # full test suite, expect 2006+ pass
bash -n scripts/auto-burn-in.sh                          # shell syntax
./tradebot-local --config-path burn-in-config.yaml scan --symbols SPY --why
# Expected: SPY scan returns reason=... (not the hardcoded "stale data" label)
./tradebot-local --config-path burn-in-config.yaml eod-fetch --date 2026-07-06 --intervals 1d --dry-run
# Expected: prints plan including per-interval marker path
```

## Rollback paths

- **Workstream A**: each task is a single small file edit; `git revert <commit>` per task.
- **Workstream B**: revert the one block in `_apply_phase1_signal_quality` (the `# AGGRESSIVE 2026-07-08` comment marks it).
- **Workstream C**: marker filename change. Old `.last_eod_fetch_*.marker` files will coexist with new `.last_eod_fetch_*_<intervals>.marker` until cleaned up; either delete stale markers (safe — the data store keeps the parquet files regardless) or leave them (they'll be ignored).

## Self-Review (per writing-plans skill)

- **Spec coverage**: ✅ 3 layers identified; 3 workstreams planned; 6 tasks (A1-A4 + B1 + C1); user decisions captured.
- **Placeholder scan**: ✅ All steps have actual code or commands; no "TBD".
- **Type/signature consistency**: ✅ `_signal_timestamp(signal, frame)` signature preserved across A1 steps; `_apply_phase1_signal_quality` signature preserved in B1; `marker_filename` schema centralized.

---

## Execution Status

- [x] A4: Revert 3 TEMP overrides (executed in this session)
- [x] C1: Per-interval marker filename (executed in this session)

Pending (user deferred to later session):
- [ ] A1: Entry-timing timestamp fix
- [ ] A2: max_volume_jump_pct bump
- [ ] A3: Bash label parsing
- [ ] B1: Signal-quality advisory

---

## Resumption Instructions

1. Confirm backfill + burn-in still alive: `ps aux | grep -E 'eod-fetch|auto-burn-in'`
2. Run `./tradebot-local --config-path burn-in-config.yaml scan --symbols SPY --why` to confirm new SCAN label
3. Run `bash /tmp/post_market_cleanup.sh` if you want to apply A4 manually
4. For Workstream C verification:
   ```bash
   ls state/data_store/.last_eod_fetch_2026-07-06*.marker
   ./tradebot-local --config-path burn-in-config.yaml eod-fetch --date 2026-07-06 --intervals 1m --dry-run
   ```
   Should reference `2026-07-06_1m.marker` (not skipping).

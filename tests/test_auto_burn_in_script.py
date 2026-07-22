import json
import os
import subprocess
from pathlib import Path


def test_auto_burn_in_runs_nightly_tune_after_daily_discovery() -> None:
    script = Path("scripts/auto-burn-in.sh").read_text(encoding="utf-8")

    assert "run_nightly_tuning()" in script
    assert 'sh ./tradebot-local --config-path "$CONFIG_FILE" tune' in script
    assert 'run_discovery "daily"' in script
    assert 'run_nightly_tuning' in script


def test_auto_burn_in_runs_tune_experiment_evaluate_in_nightly() -> None:
    script = Path("scripts/auto-burn-in.sh").read_text(encoding="utf-8")
    assert "run_tune_experiment_step" in script
    assert 'sh ./tradebot-local --config-path "$CONFIG_FILE" tune-experiment evaluate' in script
    assert 'sh ./tradebot-local --config-path "$CONFIG_FILE" tune-experiment propose' in script


def test_auto_burn_in_runs_advisory_learner_periodically_and_on_shutdown() -> None:
    script = Path("scripts/auto-burn-in.sh").read_text(encoding="utf-8")

    assert 'advisory-learn' in script
    assert '--daily-report' in script
    assert 'trap ' in script
    assert 'ADVISORY_ENABLED=' in script


def test_auto_burn_in_integrates_dashboard_sidecar() -> None:
    """auto-burn-in.sh must start and stop the dashboard as a sidecar."""
    script = Path("scripts/auto-burn-in.sh").read_text(encoding="utf-8")

    # Lifecycle helpers exist and are wired into startup + shutdown + main loop.
    assert "ensure_dashboard()" in script
    assert "stop_dashboard()" in script
    assert "on_shutdown" in script and "stop_dashboard" in script.split("on_shutdown", 1)[1]
    assert script.count("ensure_dashboard") >= 2  # startup + per-cycle restart

    # Default-on with documented opt-out via env vars.
    assert 'AUTO_DASHBOARD="${AUTO_DASHBOARD:-true}"' in script
    assert 'DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"' in script
    # DASHBOARD_PORT must be EXPORTED so child processes (doctor --burn-in)
    # see the same port the burn-in's sidecar started on.
    assert "export DASHBOARD_PORT" in script

    # Uses the launcher script and points it at the same config the burn-in uses.
    assert "./scripts/start-dashboard.sh" in script
    assert '--config "$CONFIG_FILE"' in script
    assert '--port "$DASHBOARD_PORT"' in script

    # Operator-facing hint mentions the URL + log file.
    assert "DASHBOARD_PORT" in script
    assert "DASHBOARD_LOG" in script


def test_auto_burn_in_integrates_eod_data_download() -> None:
    """auto-burn-in.sh must schedule the EOD massive.com data download.

    Audit item 7: previously the call lived inside the daily-discovery
    branch (around 09:30 ET). If the first cycle ran before the 11:30 ET
    time gate, the EOD fetch was skipped for the rest of the day. The
    fetch is now called once per main loop iteration; the function
    itself gates on the per-interval marker and time window, so
    repeated calls are idempotent.
    """
    script = Path("scripts/auto-burn-in.sh").read_text(encoding="utf-8")

    # Function defined + called.
    assert "run_eod_data_download()" in script
    assert script.count("run_eod_data_download") >= 2  # definition + invocation
    # The call must appear in the main loop (outside any discovery if-block).
    # Use the line marker "write_heartbeat" right above the call to delimit
    # the main loop body without confusing it with nested loops.
    heartbeat_idx = script.rfind("write_heartbeat")
    sleep_until_idx = script.find("sleep_until_market_open", heartbeat_idx)
    main_loop_segment = script[heartbeat_idx:sleep_until_idx]
    eod_count_in_loop = main_loop_segment.count("run_eod_data_download")
    assert eod_count_in_loop >= 1, (
        "run_eod_data_download must be called inside the main loop so it "
        "fires every iteration, not only when discovery runs"
    )

    # Default-on with documented opt-out via env var.
    assert 'EOD_DATA_STORE="${EOD_DATA_STORE:-true}"' in script

    # Idempotency marker file (per-interval-set, since C1 2026-07-08).
    assert "EOD_INTERVALS_SLUG" in script
    assert "EOD_STORE_ROOT" in script
    # Time gate so we don't run before massive.com publishes (≈11:00 ET).
    assert "EOD_FETCH_TIME" in script
    # Logs to a dedicated file under LOG_DIR.
    assert 'EOD_FETCH_LOG="$LOG_DIR/eod_data_store.log"' in script

    # CLI invocation mirrors the burn-in config path.
    assert "./tradebot-local --config-path \"$CONFIG_FILE\" eod-fetch" in script

    # CRITICAL: must pass YESTERDAY's date (massive.com publishes day-T's
    # bars at 11:00 ET on day T+1). Using today's date yields a 404 every
    # single day. Either omit the flag (let CLI default to yesterday) or
    # pass an explicit "yesterday" computation.
    # NOTE: use the function's closing "}" via "    return 0" followed by
    # the brace — naive first-"}" splitting breaks when the function uses
    # ${VAR} parameter expansion.
    fetch_block = script.split("run_eod_data_download()", 1)[1].split("\n}\n", 1)[0]
    assert "yesterday" in fetch_block or "v-1d" in fetch_block, (
        "EOD fetch must target YESTERDAY (massive.com publishes day-T's bars "
        "at ~11:00 ET on day T+1), not today"
    )

    # CRITICAL: must not mark the day complete if every fetch failed.
    # Otherwise a transient S3 outage locks out retries on the same date.
    assert "total_partitions=0" in fetch_block, (
        "shell must skip the marker when no partitions were written, so "
        "future runs retry the same date"
    )


def test_write_heartbeat_creates_json(tmp_path: Path, monkeypatch):
    """The write_heartbeat shell helper writes valid JSON containing ts + cycle."""
    # Run a tiny bash script that sources the function and writes once.
    script = """
    set -e
    CYCLE_COUNT=7
    STATE_DIR="$STATE_DIR"
    mkdir -p "$STATE_DIR/burn_in"
    HEALTH_STATE_DIR="$STATE_DIR/burn_in"
    HEARTBEAT_FILE="$HEALTH_STATE_DIR/heartbeat.json"
    write_heartbeat() {
        local fills="$1" exits="$2" rejects="$3"
        local ts_iso
        ts_iso=$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')
        cat > "$HEARTBEAT_FILE" <<EOF
{"ts":"$ts_iso","cycle":$CYCLE_COUNT,"fills":$fills,"exits":$exits,"rejects":$rejects}
EOF
    }
    write_heartbeat 1 2 3
    cat "$HEARTBEAT_FILE"
    """
    env = os.environ.copy()
    env["STATE_DIR"] = str(tmp_path)
    proc = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["cycle"] == 7
    assert payload["fills"] == 1
    assert payload["exits"] == 2
    assert payload["rejects"] == 3
    assert "ts" in payload


def test_eod_watchdog_writes_pid_file(tmp_path: Path):
    """start_eod_watchdog should persist its PID to EOD_WATCHDOG_PID_FILE."""
    # We can't actually start the watchdog (it loops forever). Instead,
    # verify the line `echo "$EOD_WATCHDOG_PID" > "$EOD_WATCHDOG_PID_FILE"`
    # exists in auto-burn-in.sh. This guards against accidental deletion.
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "auto-burn-in.sh"
    contents = script_path.read_text()
    assert "EOD_WATCHDOG_PID_FILE" in contents
    assert (
        'echo "$EOD_WATCHDOG_PID" > "$EOD_WATCHDOG_PID_FILE"' in contents
    ), "start_eod_watchdog must persist its PID for the health check"


def test_run_health_check_function_present():
    """run_health_check shell function must be defined in auto-burn-in.sh."""
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "auto-burn-in.sh"
    contents = script_path.read_text()
    assert "run_health_check()" in contents
    assert "doctor --burn-in --json" in contents


def test_no_local_outside_functions_in_burn_in_script() -> None:
    """Regression: 'local' is only valid inside bash functions.

    2026-07-13 incident — auto-burn-in.sh used 'local pre_eod_h=...' at the
    bottom of the main while-loop, which is top-level code, not a function.
    The script crashed with:

        ./scripts/auto-burn-in.sh: line 1091: local: can only be used in a function

    on every cycle once execution reached that block. Both fills that morning
    (BKSY + CDNS) lost their EOD exit because the crash happened between
    paper-trade (9:30:45 ET) and the 15:55 ET safety net.

    This test parses the script as a brace-depth scanner: 'local' is only
    legal at brace_depth > 0 (i.e. inside a function body). It is NOT a
    full bash parser — heredocs, eval'd strings, and case-branch bodies
    inside functions are best-effort approximations — but it catches the
    class of bug we just saw.
    """
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "auto-burn-in.sh"
    contents = script_path.read_text(encoding="utf-8")

    violations: list[tuple[int, str]] = []
    brace_depth = 0
    in_heredoc_marker: str | None = None
    in_heredoc_count = 0  # lines remaining until heredoc terminator

    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()

        # Skip blank lines and comments.
        if not line or line.startswith("#"):
            continue

        # Track heredoc state. Heredoc markers look like:
        #     cat > "$FILE" <<EOF
        # We start a heredoc and consume all lines until we see the marker.
        if in_heredoc_marker is not None:
            if line == in_heredoc_marker:
                in_heredoc_marker = None
            continue
        if "<<" in line:
            # Crude marker extractor: <<EOF, <<'EOF', <<-EOF, <<- 'EOF'
            after = line.split("<<", 1)[1].lstrip("-").strip().strip("'\"")
            # The marker may be on a later line (e.g. `cat <<EOF`), but most
            # uses in auto-burn-in.sh are inline. Take the first whitespace-
            # delimited token after <<.
            marker = after.split()[0] if after else ""
            if marker and marker.isidentifier():
                in_heredoc_marker = marker
                continue

        # Track brace depth at top-level (leading whitespace matters here):
        # bash function definitions look like `func_name() {` at column 0
        # and the closing `}` is also at column 0. Inside the function body,
        # control structures add one brace level; we don't need to count
        # those accurately — only need to know "are we inside a function?".
        leading_spaces = len(raw_line) - len(raw_line.lstrip())

        if leading_spaces == 0 and line.endswith("{"):
            brace_depth += 1
            continue
        if leading_spaces == 0 and line == "}":
            brace_depth = max(brace_depth - 1, 0)
            continue

        # Detect top-level 'local' usage. We only flag it when the line
        # is NOT inside a function (brace_depth == 0). Inside functions
        # (brace_depth >= 1) `local` is the correct keyword.
        if brace_depth == 0 and (line.startswith("local ") or line == "local"):
            violations.append((line_number, raw_line))

    assert not violations, (
        "Found 'local' declarations outside any function in "
        "scripts/auto-burn-in.sh:\n"
        + "\n".join(f"  line {n}: {line!r}" for n, line in violations)
        + "\n\n'local' is only valid inside bash function bodies. Drop the "
        "'local' keyword for top-level variables."
    )


def test_pre_eod_block_in_main_loop_uses_plain_assignments() -> None:
    """Targeted regression: pre_eod_* declarations must be plain assignments.

    The 2026-07-13 incident was specifically about these three lines.
    Even if someone reorders or refactors the surrounding code, these
    variables must not pick up a 'local' prefix.
    """
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "auto-burn-in.sh"
    contents = script_path.read_text(encoding="utf-8")

    for var in ("pre_eod_h", "pre_eod_m", "pre_eod_dow"):
        # Find any line that assigns the variable, optionally prefixed
        # with 'local' — that's exactly the bug we want to catch.
        matches = [
            line.strip()
            for line in contents.splitlines()
            if line.lstrip().startswith(f"{var}=")
            or line.lstrip().startswith(f"local {var}=")
        ]
        assert matches, f"{var} must be assigned in auto-burn-in.sh"
        assert len(matches) == 1, f"{var} assigned multiple times: {matches!r}"
        line = matches[0]
        assert not line.startswith("local "), (
            f"{var} must not be 'local' (top-level code, not a function): "
            f"{line!r} — the 2026-07-13 burn-in crash started here."
        )

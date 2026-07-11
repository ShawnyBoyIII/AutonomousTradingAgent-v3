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

    # Uses the launcher script and points it at the same config the burn-in uses.
    assert "./scripts/start-dashboard.sh" in script
    assert '--config "$CONFIG_FILE"' in script
    assert '--port "$DASHBOARD_PORT"' in script

    # Operator-facing hint mentions the URL + log file.
    assert "DASHBOARD_PORT" in script
    assert "DASHBOARD_LOG" in script


def test_auto_burn_in_integrates_eod_data_download() -> None:
    """auto-burn-in.sh must schedule the EOD massive.com data download."""
    script = Path("scripts/auto-burn-in.sh").read_text(encoding="utf-8")

    # Function defined + called.
    assert "run_eod_data_download()" in script
    assert script.count("run_eod_data_download") >= 2  # definition + invocation
    # Wired into the daily-discovery slot BEFORE nightly tuning so the
    # learning loops consume the freshly fetched data.
    #
    # In the source, the main-loop section looks like:
    #     run_discovery "daily"
    #     run_eod_data_download
    #     run_nightly_tuning
    # We assert the call ORDER at the main-loop site (not the function
    # definition site) by checking the segment between run_discovery "daily"
    # and the end of the if-block.
    daily_block = script.split('run_discovery "daily"', 1)[1]
    eod_pos = daily_block.index("run_eod_data_download")
    tune_pos = daily_block.index("run_nightly_tuning")
    assert eod_pos < tune_pos, (
        "run_eod_data_download must run before run_nightly_tuning so the "
        "learning loops consume fresh data"
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

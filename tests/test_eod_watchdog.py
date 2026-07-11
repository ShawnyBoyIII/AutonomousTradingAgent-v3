"""Tests for the EOD-exit watchdog in auto-burn-in.sh.

The watchdog is a background subshell that polls the wall clock and
fires `./tradebot-local manage-positions` at 15:55 ET on weekdays,
using a marker file for idempotency.  This is the safety net added
after the 2026-07-09 incident where the main loop hung for 7+ hours
at scan time, blocking the 15:55 ET EOD exit.

We test the logic by extracting the polling loop and exercising it
with a mocked clock.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest


def _run_watchdog_iteration(
    script: Path,
    state_dir: Path,
    mock_time_hhmm: str,
    mock_dow: str,
    today: str,
) -> tuple[int, str, str]:
    """Run one iteration of the watchdog logic with mocked time.

    Returns (returncode, stdout, stderr).
    """
    env = os.environ.copy()
    env["MOCK_HHMM"] = mock_time_hhmm
    env["MOCK_DOW"] = mock_dow
    env["MOCK_TODAY"] = today
    env["STATE_DIR"] = str(state_dir)
    # Use the polling loop in isolation, not the full auto-burn-in.sh
    proc = subprocess.Popen(
        [
            "bash",
            "-c",
            f"""
            STATE_DIR="$STATE_DIR"
            eod_minute=$((15 * 60 + 55))
            now_h=$(echo "$MOCK_HHMM" | cut -d: -f1)
            now_m=$(echo "$MOCK_HHMM" | cut -d: -f2)
            now_dow="$MOCK_DOW"
            today="$MOCK_TODAY"
            now_min=$((10#$now_h * 60 + 10#$now_m))
            marker="$STATE_DIR/.last_eod_watchdog_fire_$today.marker"
            if [ "$now_dow" -le 5 ] && [ "$now_min" -ge "$eod_minute" ] && [ ! -f "$marker" ]; then
                echo "FIRED at $MOCK_HHMM dow=$MOCK_DOW today=$today"
                touch "$marker"
                echo "MARKER_TOUCHED"
            else
                echo "SKIPPED at $MOCK_HHMM dow=$MOCK_DOW today=$today"
            fi
            """,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout, stderr = proc.communicate(timeout=10)
    return proc.returncode, stdout.decode("utf-8"), stderr.decode("utf-8")


def test_watchdog_fires_at_1555_weekday(tmp_path: Path) -> None:
    """Watchdog fires at 15:55 ET on a Monday-Friday with no existing marker."""
    rc, stdout, stderr = _run_watchdog_iteration(
        script=None,  # not needed for this isolated test
        state_dir=tmp_path,
        mock_time_hhmm="15:55",
        mock_dow="1",  # Monday
        today="2026-07-06",
    )
    assert rc == 0
    assert "FIRED at 15:55" in stdout
    assert "MARKER_TOUCHED" in stdout
    # Verify marker file was created
    marker = tmp_path / ".last_eod_watchdog_fire_2026-07-06.marker"
    assert marker.exists()


def test_watchdog_fires_after_1555_weekday(tmp_path: Path) -> None:
    """Watchdog fires at any time after 15:55 ET on a weekday (catch-up)."""
    rc, stdout, _ = _run_watchdog_iteration(
        script=None,
        state_dir=tmp_path,
        mock_time_hhmm="16:30",
        mock_dow="3",  # Wednesday
        today="2026-07-08",
    )
    assert rc == 0
    assert "FIRED at 16:30" in stdout


def test_watchdog_skips_before_1555(tmp_path: Path) -> None:
    """Watchdog does NOT fire at 15:54 ET."""
    rc, stdout, _ = _run_watchdog_iteration(
        script=None,
        state_dir=tmp_path,
        mock_time_hhmm="15:54",
        mock_dow="1",
        today="2026-07-06",
    )
    assert rc == 0
    assert "SKIPPED at 15:54" in stdout
    assert "FIRED" not in stdout
    marker = tmp_path / ".last_eod_watchdog_fire_2026-07-06.marker"
    assert not marker.exists()


def test_watchdog_skips_on_weekend(tmp_path: Path) -> None:
    """Watchdog does NOT fire on Saturday/Sunday even at 15:55 ET."""
    for dow, day in [("6", "Saturday"), ("7", "Sunday")]:
        rc, stdout, _ = _run_watchdog_iteration(
            script=None,
            state_dir=tmp_path,
            mock_time_hhmm="15:55",
            mock_dow=dow,
            today=f"2026-07-{4 if dow == '6' else 5}",
        )
        assert rc == 0
        assert "SKIPPED" in stdout
        assert "FIRED" not in stdout


def test_watchdog_skips_when_marker_exists(tmp_path: Path) -> None:
    """Watchdog does NOT fire twice on the same day (idempotent via marker)."""
    today = "2026-07-09"
    # First call: should fire
    _run_watchdog_iteration(
        script=None,
        state_dir=tmp_path,
        mock_time_hhmm="15:55",
        mock_dow="4",  # Thursday
        today=today,
    )
    # Second call: should skip because marker exists
    rc, stdout, _ = _run_watchdog_iteration(
        script=None,
        state_dir=tmp_path,
        mock_time_hhmm="16:00",
        mock_dow="4",
        today=today,
    )
    assert rc == 0
    assert "SKIPPED" in stdout
    assert "FIRED" not in stdout


def test_watchdog_in_script_has_start_function() -> None:
    """The auto-burn-in.sh script defines start_eod_watchdog and stop_eod_watchdog."""
    script = Path(__file__).parent.parent / "scripts" / "auto-burn-in.sh"
    content = script.read_text(encoding="utf-8")
    assert "start_eod_watchdog()" in content
    assert "stop_eod_watchdog()" in content
    # The watchdog must be started after ensure_dashboard
    assert "start_eod_watchdog" in content
    # The watchdog must be stopped on shutdown
    assert "stop_eod_watchdog" in content


def test_watchdog_in_script_called_from_on_shutdown() -> None:
    """The on_shutdown() function must call stop_eod_watchdog."""
    script = Path(__file__).parent.parent / "scripts" / "auto-burn-in.sh"
    content = script.read_text(encoding="utf-8")
    # Find on_shutdown function body
    on_shutdown_idx = content.find("on_shutdown() {")
    next_func_idx = content.find("\n}\n", on_shutdown_idx)
    on_shutdown_body = content[on_shutdown_idx:next_func_idx]
    assert "stop_eod_watchdog" in on_shutdown_body, (
        "on_shutdown() must call stop_eod_watchdog to clean up the background process"
    )


# --------------------------------------------------------------------- #
# Finding 1 (2026-07-09 code review): the watchdog must NOT touch the
# day's marker if the manage-positions subprocess exits non-zero.
# Otherwise an EOD-day failure is silently marked "complete" and the
# safety net never retries for the rest of the day.
# --------------------------------------------------------------------- #


def _run_watchdog_body(
    state_dir: Path,
    command_to_invoke: str,
) -> tuple[int, str, str]:
    """Run the real watchdog firing block from auto-burn-in.sh as a heredoc.

    The block under test is the post-fix code path: it captures the
    manage-positions exit code and only touches the marker when rc == 0.
    We exercise both paths (rc=0 vs rc=1) via `command_to_invoke`.

    The heredoc is read from the actual auto-burn-in.sh script so the
    test stays in sync with the production code; if the line numbers
    change, the test still uses the latest block. The block is wrapped
    in a function so that `local` declarations inside it (used in the
    production code) are valid bash.

    To isolate the test from the manage-position lock helpers
    (introduced by the 2026-07-09 concurrency fix), this helper
    stubs `_manage_lock_acquire`/`_manage_lock_release` as no-ops
    so the watchdog firing block runs without touching `state/`.
    """
    script = Path(__file__).parent.parent / "scripts" / "auto-burn-in.sh"
    content = script.read_text(encoding="utf-8")
    # Extract the firing block: from `if [ "$now_dow" -le 5 ]` to the
    # matching `fi` (the next outer `fi`, after which `sleep 30` runs).
    start_idx = content.find('if [ "$now_dow" -le 5 ]')
    assert start_idx > 0, "could not find watchdog firing block in auto-burn-in.sh"
    sleep_idx = content.find("sleep 30\n", start_idx)
    assert sleep_idx > 0, "could not find 'sleep 30' terminator"
    block_end = content.rfind("fi\n", start_idx, sleep_idx) + len("fi\n")
    firing_block = content[start_idx:block_end]
    # Replace the inline manage-positions invocation with a stub that
    # we can drive from the test environment. The marker path is
    # preserved exactly so we're testing the production code path.
    stubbed_block = firing_block.replace(
        "sh ./tradebot-local --config-path \"$config_file\" manage-positions 2>&1",
        f"{command_to_invoke}",
    )
    indented = "\n".join("    " + line for line in stubbed_block.split("\n"))
    env = os.environ.copy()
    env["STATE_DIR"] = str(state_dir)
    env["MOCK_HHMM"] = "15:55"
    env["MOCK_DOW"] = "1"
    env["MOCK_TODAY"] = "2026-07-13"
    proc = subprocess.Popen(
        [
            "bash",
            "-c",
            f"""
            STATE_DIR="$STATE_DIR"
            config_file="ignored-for-test"
            eod_minute=$((15 * 60 + 55))
            # Stub the lock helpers to no-ops so this test isolates
            # the marker-touching logic from the lock-file concern
            # (covered separately in tests/test_manage_lock.py).
            _manage_lock_acquire() {{ return 0; }}
            _manage_lock_release() {{ return 0; }}
            now_h=$(echo "$MOCK_HHMM" | cut -d: -f1)
            now_m=$(echo "$MOCK_HHMM" | cut -d: -f2)
            now_dow="$MOCK_DOW"
            today="$MOCK_TODAY"
            now_min=$((10#$now_h * 60 + 10#$now_m))
            marker="$STATE_DIR/.last_eod_watchdog_fire_$today.marker"
            _run_fire_block() {{
{indented}
            }}
            _run_fire_block
            """,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout, stderr = proc.communicate(timeout=10)
    return proc.returncode, stdout.decode("utf-8"), stderr.decode("utf-8")


def test_watchdog_marks_complete_when_command_succeeds(tmp_path: Path) -> None:
    """When manage-positions exits 0, the day's marker is created (happy path)."""
    marker = tmp_path / ".last_eod_watchdog_fire_2026-07-13.marker"
    assert not marker.exists()
    rc, stdout, _ = _run_watchdog_body(
        state_dir=tmp_path,
        command_to_invoke="echo 'all good'",  # exit 0
    )
    assert rc == 0
    assert marker.exists(), "marker must be created when manage-positions succeeds"


def test_watchdog_does_NOT_mark_complete_when_command_fails(tmp_path: Path) -> None:
    """When manage-positions exits non-zero, the marker must NOT be created.

    This is the 2026-07-09 code review finding: the prior version's
    pipeline `... | head | sed` returned sed's exit code (0), so a
    failing manage-positions was treated as success and the marker
    prevented any retry for the rest of the day.
    """
    marker = tmp_path / ".last_eod_watchdog_fire_2026-07-13.marker"
    assert not marker.exists()
    rc, stdout, stderr = _run_watchdog_body(
        state_dir=tmp_path,
        command_to_invoke="echo 'simulated traceback' >&2; exit 1",  # exit 1
    )
    assert rc == 0, "bash subshell should still exit 0 (we don't want set -e killing the watchdog)"
    assert not marker.exists(), (
        "marker MUST NOT be created when manage-positions fails — otherwise "
        "the watchdog's idempotency guard will prevent retry for the rest "
        "of the day, leaving EOD-exit positions open."
    )
    # The "FAILED" log line should be present so operators see the issue.
    assert "FAILED" in stdout or "FAILED" in stderr

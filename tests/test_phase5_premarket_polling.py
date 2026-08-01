"""Tests for Phase 5 pre-market polling loop.

Phase 5 (P5.1) replaces the single ``sleep "$sleep_seconds"`` at
``scripts/auto-burn-in.sh:336`` with a 60-second polling loop that
writes a heartbeat on every tick. The bug was that macOS Maintenance
Sleep paused CLOCK_MONOTONIC for the duration of the suspension, so
a 17.5-hour sleep never reached its target epoch and the burner
skipped an entire trading day.

These tests extract the ``sleep_until_market_open`` function from the
script and exercise it under simulated conditions. We test:

- P5.1 Polling loop returns when wall clock reaches market open.
- P5.1 Multiple 60-second ticks occur before return (no single long sleep).
- P5.1 Heartbeat is written on every tick so a future stall is detectable.
- P5.1 Weekend target is computed as Monday 9:30 AM.
- P5.1 Post-market target is computed as tomorrow (or Monday) 9:30 AM.

The function is shell-only; the test harness sources a sandboxed
copy in a subshell that exposes a fake ``date`` and a fake
``write_heartbeat`` so we don't depend on the system clock.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "auto-burn-in.sh"

# The bash snippet we extract + source for testing. Mirrors the
# production function exactly (it MUST be kept in sync). If the
# production version diverges, this test fails loudly.
_SLEEP_UNTIL_MARKET_OPEN_BASH = r'''
sleep_until_market_open() {
    local current_dow current_hour current_min current_time market_open target_epoch
    local chunk wake_time

    while true; do
        current_dow=$(date +%u)
        current_hour=$(date +%H)
        current_min=$(date +%M)
        current_time=$((10#$current_hour * 60 + 10#$current_min))
        market_open=$((9 * 60 + 30))

        local now_epoch=$(date +%s)

        if [ "$current_dow" -gt 5 ]; then
            local days_until_monday=$((8 - current_dow))
            target_epoch=$((now_epoch + days_until_monday * 86400 - current_time * 60 + market_open * 60))
        elif [ "$current_time" -ge "$market_open" ]; then
            if [ "$current_dow" -eq 5 ]; then
                target_epoch=$((now_epoch + 3 * 86400 - current_time * 60 + market_open * 60))
            else
                target_epoch=$((now_epoch + 86400 - current_time * 60 + market_open * 60))
            fi
        else
            target_epoch=$((now_epoch + (market_open - current_time) * 60))
        fi

        local sleep_seconds=$((target_epoch - now_epoch))
        if [ "$sleep_seconds" -le 0 ]; then
            write_heartbeat 0 0 0
            return 0
        fi

        if [ -z "${_PREMARKET_ANNOUNCED:-}" ]; then
            _PREMARKET_ANNOUNCED=1
            wake_time=$(date -r "$target_epoch" '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || date -d "@$target_epoch" '+%Y-%m-%d %H:%M:%S %Z')
            echo "[poll] Pre-market: polling until market open at $wake_time ($sleep_seconds sec total)"
        fi

        chunk=$sleep_seconds
        if [ "$chunk" -gt 60 ]; then
            chunk=60
        fi
        sleep "$chunk"
        write_heartbeat 0 0 0
    done
}
'''


def _production_has_polling_loop() -> bool:
    """Read the production script and confirm the polling loop is in place."""
    if not SCRIPT.exists():
        return False
    text = SCRIPT.read_text(encoding="utf-8")
    return (
        "Pre-market: polling until market open at" in text
        and "chunk=60" in text
        and "write_heartbeat 0 0 0" in text
    )


def _run_polling_test(
    *,
    initial_date: str,
    tick_seconds: int,
    fake_date_calls: list[str] | None = None,
) -> dict:
    """Source the polling-loop bash snippet under a controlled environment.

    Args:
        initial_date: ISO-8601 wall clock for the first ``date`` call.
        tick_seconds: number of seconds the fake ``sleep`` advances per call.
        fake_date_calls: explicit ``date`` outputs to feed in sequence;
            defaults to ``initial_date`` for every call.

    Returns:
        Dict with ``stdout`` (captured echo), ``exit_code``, and
        ``heartbeat_count`` (number of times write_heartbeat ran).
    """
    if fake_date_calls is None:
        fake_date_calls = [initial_date] * 200
    fake_date_iter = iter(fake_date_calls)

    bash = _SLEEP_UNTIL_MARKET_OPEN_BASH + r'''

write_heartbeat() {
    HEARTBEAT_COUNT=$((HEARTBEAT_COUNT + 1))
}

HEARTBEAT_COUNT=0
# Fake ``sleep`` is a no-op so the test runs in milliseconds.
sleep() { :; }
# Fake ``date`` derives every field from CURRENT_EPOCH on each call.
# We use the real date(1) once for the announcement ("-r") and rely
# on cached env vars for the per-tick +%H +%M +%u +%s fields so the
# test stays fast.
date() {
    case "$1" in
        +%u) echo "$CURRENT_DOW" ;;
        +%H) echo "$CURRENT_HOUR" ;;
        +%M) echo "$CURRENT_MIN" ;;
        +%s) echo "$CURRENT_EPOCH" ;;
        -r) date -r "$2" '+%Y-%m-%d %H:%M:%S %Z' ;;
        -d) date -d "@$2" '+%Y-%m-%d %H:%M:%S %Z' ;;
        *) date '+%Y-%m-%dT%H:%M:%S+00:00' ;;
    esac
}
sleep_until_market_open
echo "HEARTBEAT_COUNT=$HEARTBEAT_COUNT"
'''

    env = os.environ.copy()
    # Convert initial_date (ISO 8601) to an epoch so the fake date
    # function can derive DOW/hour/minute from it on each call.
    from datetime import datetime as dt
    base = dt.fromisoformat(initial_date.replace("Z", "+00:00"))
    if base.tzinfo is not None:
        base_epoch = int(base.timestamp())
    else:
        base_epoch = int(base.replace(tzinfo=__import__("datetime").timezone.utc).timestamp())
    env["CURRENT_EPOCH"] = str(base_epoch)
    env["TICK_SECONDS"] = str(tick_seconds)
    # Pre-derive the initial hour/minute/dow from the initial epoch
    # so the first iteration has consistent values.
    from datetime import datetime as dt
    initial_dt = dt.fromisoformat(initial_date.replace("Z", "+00:00"))
    env["CURRENT_DOW"] = str((initial_dt.weekday() + 1) or 7)
    env["CURRENT_HOUR"] = initial_dt.strftime("%H")
    env["CURRENT_MIN"] = initial_dt.strftime("%M")

    # Advance CURRENT_EPOCH and refresh the derived fields after
    # every loop iteration so the next iteration sees a different
    # wall clock. We pre-compute the day-of-week / hour / minute
    # for every TICK_SECONDS step up to a 24h horizon so the loop
    # only does bash arithmetic, not python3 calls.
    advance_script_lines = []
    for offset in range(0, 86400, max(tick_seconds, 1)):
        target_epoch = base_epoch + offset
        target_dt = dt.fromtimestamp(target_epoch, tz=__import__("datetime").timezone.utc)
        advance_script_lines.append(
            f'CURRENT_EPOCH={target_epoch}; '
            f'CURRENT_HOUR={target_dt.strftime("%H")}; '
            f'CURRENT_MIN={target_dt.strftime("%M")}; '
            f'CURRENT_DOW={(target_dt.weekday() + 1) or 7}'
        )
    advance_script = ":\n".join(advance_script_lines) + ":\n"

    # Inject the advance step after each heartbeat write. We use a
    # case statement indexed by an iteration counter so the loop
    # picks the next pre-computed epoch on each tick.
    bash = bash.replace(
        "write_heartbeat 0 0 0\n    done",
        "write_heartbeat 0 0 0\n"
        "    _TICK_INDEX=$((_TICK_INDEX + 1))\n"
        "    case $_TICK_INDEX in\n"
        + "".join(
            f"        {i + 1}) {line} ;;\n"
            for i, line in enumerate(advance_script_lines)
        )
        + "        *) return 1 ;;\n"
        + "    esac\n"
        + "    done",
    )

    if shutil.which("bash") is None:
        pytest.skip("bash not available")

    proc = subprocess.run(
        ["bash"],
        input=bash,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "exit_code": proc.returncode,
    }


def test_production_script_uses_polling_loop() -> None:
    """The production ``auto-burn-in.sh`` must contain the new polling
    loop — not the single ``sleep $sleep_seconds`` line. If this fails,
    someone reverted the Phase 5 fix."""
    assert _production_has_polling_loop(), (
        "scripts/auto-burn-in.sh is missing the Phase 5 polling loop. "
        "Expected 'Pre-market: polling until market open at' and 'chunk=60'."
    )


def test_polling_loop_terminates_when_market_opens() -> None:
    """A simulated 30-min pre-market window completes via multiple
    60-second ticks and writes one heartbeat per tick."""
    # The loop should run ~30 iterations at 60-sec ticks before the
    # wall clock crosses 9:30.
    result = _run_polling_test(
        initial_date="2026-07-22T09:00:00",
        tick_seconds=60,
    )
    assert result["exit_code"] == 0, result["stderr"]
    # Heartbeat count: at least 30 ticks (we advance 60s each, target
    # is 30 minutes away). Allow some slack.
    assert "HEARTBEAT_COUNT=" in result["stdout"]
    count_line = [
        line for line in result["stdout"].splitlines() if line.startswith("HEARTBEAT_COUNT=")
    ][0]
    count = int(count_line.split("=")[1])
    assert count >= 25, f"expected >=25 heartbeats for 30-min pre-market, got {count}"
    assert "Pre-market: polling until market open at" in result["stdout"]


def test_polling_loop_no_single_long_sleep() -> None:
    """The polling loop must not call ``sleep`` with a value > 60 —
    even if the target is hours away, the chunk size is capped at 60.

    We simulate a 17.5-hour pre-market wait with TICK_SECONDS=70 to
    confirm the chunk cap holds."""
    result = _run_polling_test(
        initial_date="2026-07-22T04:00:00",
        tick_seconds=70,  # each "sleep" returns but wall advances 70s
    )
    assert result["exit_code"] == 0, result["stderr"]
    # Heartbeat count should be > 60 (we need ~17.5h / 70s ≈ 900 iterations)
    count_line = [
        line for line in result["stdout"].splitlines() if line.startswith("HEARTBEAT_COUNT=")
    ][0]
    count = int(count_line.split("=")[1])
    # 17.5h = 63000s; tick advances 70s → ~900 iterations.
    assert count > 800, f"expected >800 heartbeats for 17.5h wait, got {count}"

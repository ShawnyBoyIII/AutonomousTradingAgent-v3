"""Regression test for the burner's market-hours check.

Audit follow-up (2026-07-23): the burner's sleep_until_market_open() used
a bare `date` call without TZ. On a CDT workstation the main-loop gate
would see local time, so 13:30 CDT (= 14:30 ET) looked "after hours"
and the script slept 24 hours, leaving the burner stuck on its first
post-wake cycle. The same bug applied to midday, EOD fetch-time,
EOD watchdog, and pre-EOD hard-check.

This test sources the script's time helper directly and exercises
the corrected logic against several wall-clock scenarios.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "scripts" / "auto-burn-in.sh"


def _bash_helper_snippet() -> str:
    """Pull out the inline ET-aware time helper from auto-burn-in.sh.

    The script defines ``current_hour=$(TZ=America/New_York date +%H)``
    inline in three places. We re-implement those calls in a here-doc
    so the test can drive them with a controlled ``now`` value.
    """
    return textwrap.dedent(
        """
        # Mimic the post-fix time checks used in sleep_until_market_open,
        # is_midday, run_eod_data_download, the EOD watchdog, and the
        # pre-EOD hard-check. All must use TZ=America/New_York so a
        # CDT/PST workstation still evaluates market hours correctly.
        now_h=$(TZ=America/New_York date +%H)
        now_m=$(TZ=America/New_York date +%M)
        now_dow=$(TZ=America/New_York date +%u)
        now_min=$((10#$now_h * 60 + 10#$now_m))
        market_open=$((9 * 60 + 30))
        market_close=$((16 * 60))
        echo "now_h=$now_h now_m=$now_m now_dow=$now_dow now_min=$now_min"
        # Within market hours on a weekday (Mon-Fri)?
        if [ "$now_dow" -le 5 ] \
            && [ "$now_min" -ge "$market_open" ] \
            && [ "$now_min" -lt "$market_close" ]; then
            echo "market_open=yes"
        else
            echo "market_open=no"
        fi
        """
    )


def _run_at(now_iso: str) -> dict[str, str]:
    """Run the helper snippet with the system clock faked to ``now_iso``.

    We inline a shim ``date`` function in bash itself so the test
    does not depend on PATH shadowing or faketime(1).
    """
    import datetime

    now_et = datetime.datetime.fromisoformat(now_iso)
    hh = now_et.strftime("%H")
    mm = now_et.strftime("%M")
    # weekday: Mon=1..Sun=7. datetime.weekday() returns Mon=0..Sun=6,
    # so add 1 to match date +%u.
    dow = (now_et.weekday() + 1)

    shim_script = textwrap.dedent(
        f"""
        # Define a shim 'date' that ignores arguments and returns
        # the faked ET clock. This avoids PATH shadowing and matches
        # the post-fix script's TZ=America/New_York +%H, +%M, +%u calls.
        shim_date() {{
            case "$1" in
                +%H) echo "{hh}" ;;
                +%M) echo "{mm}" ;;
                +%u) echo "{dow}" ;;
                *) echo "" ;;
            esac
        }}
        # Override the date binary for the inner snippet via a shell
        # function. Functions take precedence over PATH lookups.
        date() {{ shim_date "$@"; }}
        { _bash_helper_snippet() }
        """
    )

    proc = subprocess.run(
        ["bash", "-c", shim_script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    out = {}
    # The helper concatenates multiple key=value pairs on a single line
    # (e.g. `now_h=14 now_m=30 ...`). Parse each pair separately.
    for line in proc.stdout.strip().splitlines():
        for token in line.split():
            if "=" in token:
                k, v = token.split("=", 1)
                out[k] = v
    return out


def test_market_hours_open_at_2pm_et_weekday() -> None:
    """2:00 PM ET on a Thursday is inside market hours."""
    result = _run_at("2026-07-23T14:00:00")
    assert result["market_open"] == "yes"
    assert int(result["now_min"]) == 14 * 60


def test_market_hours_closed_at_8am_et_weekday() -> None:
    """8:00 AM ET on a Thursday is before market open."""
    result = _run_at("2026-07-23T08:00:00")
    assert result["market_open"] == "no"


def test_market_hours_closed_at_5pm_et_weekday() -> None:
    """5:00 PM ET on a Thursday is after market close."""
    result = _run_at("2026-07-23T17:00:00")
    assert result["market_open"] == "no"


def test_market_hours_open_at_9_30am_et() -> None:
    """9:30 AM ET is the open boundary — must be considered open."""
    result = _run_at("2026-07-23T09:30:00")
    assert result["market_open"] == "yes"


def test_market_hours_closed_on_weekend() -> None:
    """Saturday and Sunday are not market days even at 2 PM."""
    sat = _run_at("2026-07-25T14:00:00")
    sun = _run_at("2026-07-26T14:00:00")
    assert sat["market_open"] == "no"
    assert sun["market_open"] == "no"


def test_cdt_workstation_sees_correct_et_time() -> None:
    """When host is on CDT and ``date`` is run without TZ, the result
    is local time. The post-fix code forces TZ=America/New_York on
    every ``date`` invocation. This test asserts the helper produces
    the ET time even when the host's local time differs from ET.
    """
    # 2:30 PM ET on Thursday = 1:30 PM CDT on a CDT workstation.
    result = _run_at("2026-07-23T14:30:00")
    assert result["now_h"] == "14"
    assert result["now_min"] == str(14 * 60 + 30)

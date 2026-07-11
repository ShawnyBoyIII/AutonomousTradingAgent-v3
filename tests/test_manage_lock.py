"""Tests for code review Finding 2 (2026-07-09):

The EOD watchdog and the main burn-in loop can both call
`./tradebot-local manage-positions` at 15:55 ET, producing duplicate
SELL orders in the `orders` table (the `trades`/`positions` tables
are guarded by SQLAlchemy, but the sqlite3-ledger `orders` INSERT uses
fresh UUIDs and has no atomic guard).

Fix: `mkdir`-based atomic lock directory (`state/.manage.lock`)
wrapping both `manage-positions` invocations. `flock` is not
available on macOS; `mkdir` is atomic on both macOS HFS+/APFS and
Linux ext4.

Two helpers in auto-burn-in.sh:
- `_manage_lock_acquire` — atomic mkdir + write current PID; rejects
  if held by live PID (kill -0); clears stale locks held by dead PIDs.
- `_manage_lock_release` — removes the lock directory.

Both callers (EOD watchdog at line 651, main loop at line 733) wrap
their manage-positions call in `_manage_lock_acquire ... ; _manage_lock_release`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "auto-burn-in.sh"


def _run_lock_helper(helper: str, state_dir: Path, fake_pid: int = 99999) -> tuple[int, str, str]:
    """Run a `_manage_lock_acquire` or `_manage_lock_release` call in an isolated shell.

    Returns (returncode, stdout, stderr).
    """
    env = os.environ.copy()
    env["STATE_DIR"] = str(state_dir)
    proc = subprocess.Popen(
        [
            "bash",
            "-c",
            f"""
            STATE_DIR="{state_dir}"
            # Source the helpers from the script
            source <(awk '/^_manage_lock_acquire\\(\\)/{{flag=1}} flag; /^}}/{{flag=0}} flag' {SCRIPT})
            _manage_lock_acquire
            """,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout, stderr = proc.communicate(timeout=10)
    return proc.returncode, stdout.decode("utf-8"), stderr.decode("utf-8")


def _extract_helpers(content: str) -> tuple[str, str]:
    """Extract the body of `_manage_lock_acquire` and `_manage_lock_release`."""
    import re

    def grab(name: str) -> str:
        # Match `name() {` ... `}` at start of line
        m = re.search(rf"^{name}\(\) {{\n(.*?)\n}}\n", content, re.MULTILINE | re.DOTALL)
        if not m:
            raise AssertionError(f"could not find {name} function in auto-burn-in.sh")
        return m.group(1)

    # Inject `_MANAGE_LOCK_DIR` into the extracted body so the test
    # isolates don't depend on the script-level variable.
    acquire = grab("_manage_lock_acquire")
    release = grab("_manage_lock_release")
    acquire = acquire.replace(
        '$_MANAGE_LOCK_DIR',
        '"$STATE_DIR/.manage.lock"',
    )
    release = release.replace(
        '$_MANAGE_LOCK_DIR',
        '"$STATE_DIR/.manage.lock"',
    )
    return acquire, release


def test_lock_helpers_defined_in_script() -> None:
    """The script must define _manage_lock_acquire and _manage_lock_release."""
    content = SCRIPT.read_text(encoding="utf-8")
    assert "_manage_lock_acquire()" in content, "_manage_lock_acquire() must be defined"
    assert "_manage_lock_release()" in content, "_manage_lock_release() must be defined"


def test_lock_acquire_succeeds_when_no_lock_exists(tmp_path: Path) -> None:
    """First caller acquires the lock and writes its PID."""
    content = SCRIPT.read_text(encoding="utf-8")
    acquire_body, release_body = _extract_helpers(content)

    proc = subprocess.Popen(
        [
            "bash",
            "-c",
            f"""
            STATE_DIR="{tmp_path}"
            _manage_lock_acquire() {{
{acquire_body}
            }}
            _manage_lock_release() {{
{release_body}
            }}
            if _manage_lock_acquire; then
                echo "ACQUIRED"
                _manage_lock_release
                echo "RELEASED"
            else
                echo "FAILED"
            fi
            """,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0
    assert "ACQUIRED" in stdout.decode()
    assert "RELEASED" in stdout.decode()


def test_lock_acquire_fails_when_held_by_live_process(tmp_path: Path) -> None:
    """Second caller cannot acquire the lock when first caller's PID is still alive.

    Uses $$ for the live PID so we don't have to actually spawn a child process.
    """
    content = SCRIPT.read_text(encoding="utf-8")
    acquire_body, release_body = _extract_helpers(content)

    proc = subprocess.Popen(
        [
            "bash",
            "-c",
            f"""
            STATE_DIR="{tmp_path}"
            _manage_lock_acquire() {{
{acquire_body}
            }}
            _manage_lock_release() {{
{release_body}
            }}
            # First acquisition
            _manage_lock_acquire && echo "FIRST_OK" || echo "FIRST_FAIL"
            # Second acquisition (same PID, still alive) must fail
            if _manage_lock_acquire; then
                echo "SECOND_OK (BUG: lock should be held)"
            else
                echo "SECOND_FAIL (correct)"
            fi
            _manage_lock_release
            # Third acquisition after release must succeed
            _manage_lock_acquire && echo "THIRD_OK" || echo "THIRD_FAIL"
            _manage_lock_release
            """,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate(timeout=10)
    decoded = stdout.decode()
    assert "FIRST_OK" in decoded, f"first acquire failed: {decoded!r}"
    assert "SECOND_FAIL" in decoded, f"second acquire did not fail as expected: {decoded!r}"
    assert "THIRD_OK" in decoded, f"third acquire after release failed: {decoded!r}"


def test_lock_acquire_clears_stale_locks(tmp_path: Path) -> None:
    """A lock held by a dead PID (lock holder crashed) is automatically cleared."""
    content = SCRIPT.read_text(encoding="utf-8")
    acquire_body, release_body = _extract_helpers(content)

    # Pre-create a lock directory with a known dead PID (e.g. PID 1's parent
    # or a guaranteed-nonexistent PID).
    lock_dir = tmp_path / ".manage.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("99999999")  # very unlikely to be alive

    proc = subprocess.Popen(
        [
            "bash",
            "-c",
            f"""
            STATE_DIR="{tmp_path}"
            _manage_lock_acquire() {{
{acquire_body}
            }}
            if _manage_lock_acquire; then
                echo "ACQUIRED (stale lock cleared)"
                ls -la "$STATE_DIR/.manage.lock" 2>&1 | head -3
            else
                echo "FAILED (BUG: stale lock should have been cleared)"
            fi
            """,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate(timeout=10)
    decoded = stdout.decode()
    assert "ACQUIRED (stale lock cleared)" in decoded, decoded
    # The lock file should now contain the current PID, not the stale one
    pid_file = lock_dir / "pid"
    assert pid_file.exists()
    written_pid = int(pid_file.read_text().strip())
    assert written_pid != 99999999, f"stale PID {written_pid} was not replaced"


def test_eod_watchdog_uses_lock() -> None:
    """The EOD watchdog's manage-positions call must be wrapped in lock helpers."""
    content = SCRIPT.read_text(encoding="utf-8")
    # Find the firing block (within start_eod_watchdog)
    start_idx = content.find("start_eod_watchdog() {")
    assert start_idx > 0
    sleep_idx = content.find("sleep 30\n", start_idx)
    block_end = content.rfind("fi\n", start_idx, sleep_idx) + len("fi\n")
    firing_block = content[start_idx:block_end]
    # Look for the EXECUTABLE lock-call line (if ...; then), not comments.
    # Comments containing _manage_lock_acquire must not satisfy this check.
    import re
    lock_call_pattern = re.compile(r"^\s*if _manage_lock_acquire", re.MULTILINE)
    lock_match = lock_call_pattern.search(firing_block)
    assert lock_match is not None, (
        "EOD watchdog firing block must contain an executable `if _manage_lock_acquire; then` call"
    )
    # The actual manage-positions sh invocation must follow the lock call
    mp_pattern = re.compile(r"\.\/tradebot-local[^\n]*manage-positions")
    mp_match = mp_pattern.search(firing_block)
    assert mp_match is not None, "EOD watchdog firing block must invoke manage-positions"
    assert lock_match.start() < mp_match.start(), (
        "_manage_lock_acquire must precede the manage-positions call"
    )


def test_main_loop_uses_lock() -> None:
    """The main loop's manage-positions call must be wrapped in lock helpers."""
    content = SCRIPT.read_text(encoding="utf-8")
    # Find scan_and_trade's manage-positions call (around line 733)
    # The line `local manage_output=$(sh ./tradebot-local ... manage-positions)`
    # should be preceded by a _manage_lock_acquire call.
    mp_idx = content.find("manage_output=$(sh ./tradebot-local --config-path \"$CONFIG_FILE\" manage-positions")
    assert mp_idx > 0
    # Look backwards for the most recent _manage_lock_acquire
    acquire_idx = content.rfind("_manage_lock_acquire", 0, mp_idx)
    assert acquire_idx > 0, "main loop manage-positions must be preceded by _manage_lock_acquire"
    # And the release must follow
    release_idx = content.find("_manage_lock_release", mp_idx, mp_idx + 500)
    assert release_idx > 0, "main loop manage-positions must be followed by _manage_lock_release"
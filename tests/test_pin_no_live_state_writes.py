"""Regression test pinning the contract that the test suite MUST NOT
modify the live worktree's runtime state files.

**2026-07-30 incident:** A test in this suite (during a red-phase
TDD cycle for snapshot inheritance) wrote through to
``$REPO_ROOT/state/burn_in.db`` because a helper used a global
``LIVE_BURN_IN_DB`` path. The DB was modified at 09:06:03 EDT and the
PortfolioLedger tables (``portfolio_state``, ``orders``,
``equity_history``, ``kill_switch``) were dropped and replaced with
SQLAlchemy projection tables only. The $100K paper cohort (started
2026-07-28) was lost.

This regression pins the invariant: a pytest run (this entire suite)
must leave the live worktree's runtime state files byte-identical and
mtime-identical to how it found them. Any test that needs to mutate a
real DB must use ``tmp_path`` fixtures or sandbox worktrees.

**How to run:** ``pytest tests/test_pin_no_live_state_writes.py`` from
the live worktree, or as part of the full suite. The test snapshots
the live ``state/burn_in.db`` SHA + mtime before any other test in the
session runs (via a session-scoped autouse fixture) and asserts no
change at session teardown. If a test writes through, the assertion
fails with the offending ``before → after`` SHA pair so the operator
can bisect the failure.

**Opt-out:** Set ``AUTONOMOUS_TRADING_AGENT_ALLOW_LIVE_STATE_WRITES=1``
in the environment to skip the assertion. Only do this if you have a
documented reason (operator-initiated reset, integration smoke test,
etc.). This is the documented override for the operator workflow that
justifies ``burn-in-config.yaml`` state changes during a session.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_BURN_IN_DB = REPO_ROOT / "state" / "burn_in.db"
LIVE_MARKET_DATA_CACHE_DB = REPO_ROOT / "state" / "market_data_cache.db"
LIVE_UNIVERSE = REPO_ROOT / "state" / "universe.txt"

# Files under test-pin. Each entry has a human-readable label and a
# factory that returns its current ``(sha256, mtime_ns)``. We capture
# both because SQLite hot-backup swaps file contents while preserving
# inode mtimes in some configurations, and any future auto-write path
# could update only the mtime.
_PINNED_STATE_FILES = (
    ("state/burn_in.db", LIVE_BURN_IN_DB),
    ("state/market_data_cache.db", LIVE_MARKET_DATA_CACHE_DB),
    ("state/universe.txt", LIVE_UNIVERSE),
)

OVERRIDE_ENV = "AUTONOMOUS_TRADING_AGENT_ALLOW_LIVE_STATE_WRITES"


def _sha256_of(path: Path) -> str:
    if not path.exists():
        return "<missing>"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mtime_ns_of(path: Path) -> int:
    if not path.exists():
        return -1
    return path.stat().st_mtime_ns


@pytest.fixture(scope="session", autouse=True)
def _pin_live_state_before_session(request):
    """Snapshot the live state files at session start and assert
    they are byte-identical at session teardown."""
    if os.environ.get(OVERRIDE_ENV) == "1":
        yield
        return

    snapshots = {
        label: (_sha256_of(path), _mtime_ns_of(path))
        for label, path in _PINNED_STATE_FILES
    }

    yield

    drift: list[str] = []
    for label, path in _PINNED_STATE_FILES:
        before_sha, before_mtime = snapshots[label]
        after_sha = _sha256_of(path)
        after_mtime = _mtime_ns_of(path)
        if before_sha != after_sha or before_mtime != after_mtime:
            drift.append(
                f"{label}: sha {before_sha[:12]}… → {after_sha[:12]}…, "
                f"mtime {before_mtime} → {after_mtime}"
            )

    if drift:
        pytest.fail(
            "Test suite modified live worktree state files. This is the "
            "2026-07-30 regression — tests must not write through to the "
            "live worktree. Use tmp_path fixtures or sandbox worktrees.\n"
            + "\n".join(drift)
            + (
                f"\nIf this change is intentional, set {OVERRIDE_ENV}=1 "
                "in the environment and re-run."
            )
        )


def test_live_state_files_present() -> None:
    """Sanity: the pinned state files exist. If they don't, this test
    environment is the wrong place to run the pin-isolation check."""
    if os.environ.get(OVERRIDE_ENV) == "1":
        pytest.skip(f"{OVERRIDE_ENV}=1 set; pin-isolation check skipped")

    assert LIVE_BURN_IN_DB.exists(), (
        f"live worktree state/burn_in.db is missing at {LIVE_BURN_IN_DB}; "
        f"the burn-in cohort hasn't been initialized yet — run "
        f"`scripts/burnin-launcher.sh` first"
    )
    assert LIVE_MARKET_DATA_CACHE_DB.exists(), (
        f"live worktree state/market_data_cache.db is missing at "
        f"{LIVE_MARKET_DATA_CACHE_DB}"
    )


def test_pin_isolation_override_is_documented() -> None:
    """The override env var must be discoverable from the test code
    itself — operators should never have to read source to find it."""
    assert OVERRIDE_ENV.startswith("AUTONOMOUS_TRADING_AGENT_"), (
        "override env var must be project-namespaced so it doesn't "
        "collide with other tools"
    )
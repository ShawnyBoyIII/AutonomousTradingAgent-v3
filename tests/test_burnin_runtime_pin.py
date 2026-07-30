"""Regression tests for the burn-in runtime pin.

The 2026-07-24 incident: a live burner kept running while the operator
``git switch``ed the worktree. The foreground scan, paper-trade, and
manage-positions subprocesses all re-imported the live Python files,
producing a mixed-revision execution that invoked the legacy V2
``circuit_breaker`` and halted at the exact 44.9296% drawdown.

The fix is to launch the burner from an immutable snapshot of the
working tree. These tests pin that contract:

- A snapshot captured at startup must be byte-identical even after the
  live worktree is mutated.
- The wrapper ``tradebot-local`` must use the snapshot's Python source
  (via ``PYTHONPATH``) when ``PIN_DIR`` is set.
- The wrapper must fall back to the live worktree when ``PIN_DIR`` is
  unset, so manual operators are not blocked.
- The launcher script must always emit a snapshot fingerprint so an
  operator can confirm what was actually pinned.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "scripts" / "burnin-launcher.sh"
WRAPPER = REPO_ROOT / "tradebot-local"
PIN_HELPER = REPO_ROOT / "trading_bot" / "runtime" / "burnin_pin.py"
STATE_DIR = REPO_ROOT / "state"
LIVE_BURN_IN_DB = STATE_DIR / "burn_in.db"
LIVE_MARKET_DATA_CACHE_DB = STATE_DIR / "market_data_cache.db"


def _backup_live_state_files() -> dict[str, Path | None]:
    """Snapshot the live worktree state files so a test can overwrite
    them and restore in finally. Returns a mapping of relative key to
    the saved copy path (None when the source didn't exist)."""
    saved: dict[str, Path | None] = {}
    for key, src in (
        ("burn_in_db", LIVE_BURN_IN_DB),
        ("market_data_cache_db", LIVE_MARKET_DATA_CACHE_DB),
    ):
        if src.exists():
            target = Path(tempfile.mkdtemp(prefix=f"burn_in_pin_test_{key}_")) / src.name
            shutil.copy2(src, target)
            saved[key] = target
        else:
            saved[key] = None
    return saved


def _restore_live_state_files(saved: dict[str, Path | None]) -> None:
    """Restore live files from a backup dict produced by
    ``_backup_live_state_files``. Removes the live file if it didn't
    exist originally, otherwise replaces the bytes."""
    for key, src in (
        ("burn_in_db", LIVE_BURN_IN_DB),
        ("market_data_cache_db", LIVE_MARKET_DATA_CACHE_DB),
    ):
        backup = saved.get(key)
        if backup is None:
            if src.exists():
                src.unlink()
            for sibling in src.parent.glob(f"{src.name}-*"):
                sibling.unlink()
        else:
            shutil.copy2(backup, src)
            for sibling in src.parent.glob(f"{src.name}-*"):
                if sibling != src:
                    sibling.unlink()
            shutil.rmtree(backup.parent, ignore_errors=True)


def _read_portfolio_state_equity(db_path: Path) -> float:
    """Read the ``portfolio_state`` payload from a PortfolioLedger DB
    and return the JSON-parsed ``equity`` value. Returns 0.0 when the
    table is missing empty."""
    with sqlite3.connect(str(db_path)) as conn:
        try:
            cur = conn.execute("SELECT payload FROM portfolio_state WHERE id = 1")
            row = cur.fetchone()
        except sqlite3.OperationalError:
            return 0.0
    if row is None:
        return 0.0
    payload = json.loads(row[0])
    return float(payload.get("equity", 0.0))


def _set_live_burn_in_db_equity(worktree: Path, equity: float, *, cash: float | None = None) -> None:
    """Create a fresh ``state/burn_in.db`` at ``worktree/state/burn_in.db``
    with the given starting equity. Used by inheritance tests to seed a
    known cohort state. The schema matches what
    ``PortfolioLedger.initialize`` produces."""
    state_dir = worktree / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "burn_in.db"
    for sibling in sorted(state_dir.glob(f"{db_path.name}-*")):
        sibling.unlink()
    if db_path.exists():
        db_path.unlink()
    final_cash = equity if cash is None else cash
    payload = json.dumps({
        "cash": final_cash,
        "equity": equity,
        "positions": {},
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "last_exited_at": {},
    })
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS portfolio_state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO portfolio_state (id, payload) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
            (payload,),
        )
        conn.commit()


def _set_live_market_data_cache_contents(worktree: Path, rows: int) -> None:
    """Seed the ``state/market_data_cache.db`` at ``worktree/state/`` with
    ``rows`` fixture rows so inheritance tests can detect byte-for-byte
    propagation."""
    state_dir = worktree / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_path = state_dir / "market_data_cache.db"
    for sibling in sorted(state_dir.glob(f"{cache_path.name}-*")):
        sibling.unlink()
    if cache_path.exists():
        cache_path.unlink()
    with sqlite3.connect(str(cache_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                period TEXT NOT NULL,
                interval TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ttl_seconds INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        for i in range(rows):
            conn.execute(
                """
                INSERT OR REPLACE INTO cache
                (key, symbol, period, interval, created_at, ttl_seconds, expires_at, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"fixture:{i}",
                    f"SYM{i}",
                    "1d",
                    "1d",
                    "2026-07-30T00:00:00+00:00",
                    86400,
                    "2026-07-31T00:00:00+00:00",
                    "{}",
                ),
            )
        conn.commit()


def _seed_burn_in_ephemera(worktree: Path) -> None:
    """Seed ``state/burn_in/heartbeat.json``, ``burn_in.pid`` and
    ``portfolio_summary.json`` at ``worktree/state/`` so the
    no-inheritance test can detect accidental propagation."""
    state_dir = worktree / "state"
    burn_in_dir = state_dir / "burn_in"
    burn_in_dir.mkdir(parents=True, exist_ok=True)
    (burn_in_dir / "heartbeat.json").write_text(
        '{"ts": "2026-07-30T00:00:00+00:00", "cycle": 999, "fills": 99, "exits": 99, "rejects": 99}'
    )
    (burn_in_dir / "burn_in.pid").write_text("99999")
    (burn_in_dir / "portfolio_summary.json").write_text('{"inherited": true}')


def _cleanup_burn_in_ephemera(worktree: Path) -> None:
    for name in ("heartbeat.json", "burn_in.pid", "portfolio_summary.json"):
        f = worktree / "state" / "burn_in" / name
        if f.exists():
            f.unlink()


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(("git", *args), cwd=cwd, text=True)


def _make_pin_dir(tmp_path: Path) -> Path:
    pin_dir = tmp_path / "burnin_pin"
    pin_dir.mkdir()
    return pin_dir


def _extract_snapshot(tmp_path: Path) -> Path:
    """Extract HEAD into tmp_path/HEAD-trees/<sha>/ using git archive."""
    head = _git("rev-parse", "HEAD", cwd=REPO_ROOT).strip()
    extract_root = tmp_path / "trees" / head
    extract_root.mkdir(parents=True)
    archive = tmp_path / f"{head}.tar"
    subprocess.check_call(("git", "archive", "HEAD", f"-o", str(archive)), cwd=REPO_ROOT)
    subprocess.check_call(("tar", "-x", "-C", str(extract_root), "-f", str(archive)))
    return extract_root


def test_launcher_script_exists() -> None:
    assert LAUNCHER.exists(), (
        "scripts/burnin-launcher.sh must exist so the burner always launches "
        "from an immutable snapshot, not the live mutable worktree"
    )


def test_launcher_script_is_executable() -> None:
    mode = LAUNCHER.stat().st_mode
    assert mode & 0o111, "burnin-launcher.sh must be executable"


def test_launcher_captures_snapshot_to_pin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The launcher must extract HEAD into $PIN_DIR before execing the burner."""
    monkeypatch.setenv("PIN_DIR", str(_make_pin_dir(tmp_path)))
    monkeypatch.setenv("PIN_DRY_RUN", "1")  # do not actually run the burner
    monkeypatch.setenv("BURNIN_CONFIG", "config.yaml")
    result = subprocess.run(
        ("/bin/bash", str(LAUNCHER)),
        cwd=REPO_ROOT,
        env=os.environ,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"launcher failed: {result.stderr}"
    pin_dir = Path(os.environ["PIN_DIR"])
    head = _git("rev-parse", "HEAD", cwd=REPO_ROOT).strip()
    snapshot_root = pin_dir / head
    assert snapshot_root.exists(), f"snapshot dir {snapshot_root} was not created"
    assert (snapshot_root / "scripts" / "auto-burn-in.sh").exists()
    assert (snapshot_root / "tradebot-local").exists()


def test_pin_helper_archives_head_and_records_sha(tmp_path: Path) -> None:
    """burnin_pin.capture_snapshot extracts HEAD into a stable subdir."""
    from trading_bot.runtime.burnin_pin import capture_snapshot

    pin_dir = _make_pin_dir(tmp_path)
    info = capture_snapshot(REPO_ROOT, pin_dir)
    assert info.snapshot_root.exists()
    assert info.snapshot_root == pin_dir / info.head_sha
    assert (info.snapshot_root / "scripts" / "auto-burn-in.sh").exists()
    assert (info.snapshot_root / "tradebot-local").exists()
    # ``.venv/bin/`` is created as an empty stub so the wrapper can
    # resolve the python executable path; the actual python binary is
    # not copied unless ``BURNIN_PIN_COPY_VENV`` is set.
    assert (info.snapshot_root / ".venv" / "bin").exists()
    # The fingerprint is the SHA256 of the scripts we depend on, not the
    # entire tree, so that legitimate config mutations inside the
    # snapshot are still reflected in the fingerprint.
    assert len(info.fingerprint) == 64
    # Re-running the capture must produce the same head + fingerprint.
    info2 = capture_snapshot(REPO_ROOT, pin_dir)
    assert info.head_sha == info2.head_sha
    assert info.fingerprint == info2.fingerprint


def test_pin_snapshot_is_immutable_to_live_mutation(tmp_path: Path) -> None:
    """If the live worktree changes, the snapshot MUST NOT change."""
    from trading_bot.runtime.burnin_pin import capture_snapshot

    pin_dir = _make_pin_dir(tmp_path)
    info = capture_snapshot(REPO_ROOT, pin_dir)
    snapshot_auto_burn = info.snapshot_root / "scripts" / "auto-burn-in.sh"
    snapshot_wrapper = info.snapshot_root / "tradebot-local"
    snapshot_burn_in_db = info.snapshot_root / "state" / "burn_in.db"
    snapshot_auto_burn_sha = hashlib.sha256(snapshot_auto_burn.read_bytes()).hexdigest()
    snapshot_wrapper_sha = hashlib.sha256(snapshot_wrapper.read_bytes()).hexdigest()
    snapshot_burn_in_db_sha = (
        hashlib.sha256(snapshot_burn_in_db.read_bytes()).hexdigest()
        if snapshot_burn_in_db.exists()
        else None
    )

    # Mutate the live worktree to a "REVISION B" state.
    (REPO_ROOT / "scripts" / "auto-burn-in.sh").write_text(
        "# REVISION_B_MUTATION\n" + snapshot_auto_burn.read_text()
    )
    (REPO_ROOT / "tradebot-local").write_text(
        "# REVISION_B_MUTATION\n" + snapshot_wrapper.read_text()
    )
    try:
        # The snapshot must NOT reflect the mutation.
        assert (
            hashlib.sha256(snapshot_auto_burn.read_bytes()).hexdigest()
            == snapshot_auto_burn_sha
        )
        assert (
            hashlib.sha256(snapshot_wrapper.read_bytes()).hexdigest()
            == snapshot_wrapper_sha
        )
        # The snapshot does not contain the mutation marker.
        assert "REVISION_B_MUTATION" not in snapshot_auto_burn.read_text()
        # (2026-07-30) The snapshot's state/burn_in.db is inherited from
        # the live worktree at capture time. A post-capture mutation of
        # the live DB must NOT change the snapshot's frozen copy.
        if snapshot_burn_in_db_sha is not None:
            assert (
                hashlib.sha256(snapshot_burn_in_db.read_bytes()).hexdigest()
                == snapshot_burn_in_db_sha
            ), (
                "snapshot's inherited state/burn_in.db must be immutable "
                "to post-capture mutations of the live DB"
            )
    finally:
        # Restore the live worktree so the rest of the suite is unaffected.
        subprocess.check_call(("git", "checkout", "--", "scripts/auto-burn-in.sh", "tradebot-local"), cwd=REPO_ROOT)


def _make_worktree_isolated_repo(tmp_path: Path) -> Path:
    """For inheritance tests we need a controlled repository whose
    ``state/burn_in.db`` can be mutated without touching the live worktree.
    Building a fresh git repo at tmp_path is heavy but reproducible; the
    alternative of mutating the live worktree would race the running
    burner. Copy the minimal source tree required for
    ``capture_snapshot`` (the Python helper only reads the code via
    ``git archive HEAD`` and writes to its output dir) into a tmp
    git repo and use that."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    subprocess.check_call(("git", "init", "--quiet", str(worktree)), cwd=worktree)
    subprocess.check_call(("git", "config", "user.email", "test@example.com"), cwd=worktree)
    subprocess.check_call(("git", "config", "user.name", "test"), cwd=worktree)
    # Stage a tracked file so HEAD resolves and git archive has content.
    (worktree / "README.md").write_text("fixture\n")
    subprocess.check_call(("git", "add", "README.md"), cwd=worktree)
    subprocess.check_call(("git", "commit", "--quiet", "-m", "init"), cwd=worktree)
    return worktree


def test_pin_snapshot_inherits_live_burn_in_db_equity(tmp_path: Path) -> None:
    """Regression for 2026-07-30 cohort divergence: the snapshot's
    ``state/burn_in.db`` must copy the live cohort's ``portfolio_state``
    at capture, so the pinned burner inherits the $100K reset cohort
    instead of re-initializing a $10K default."""
    from trading_bot.runtime.burnin_pin import capture_snapshot

    worktree = _make_worktree_isolated_repo(tmp_path)
    pin_dir = tmp_path / "burnin_pin"
    pin_dir.mkdir()
    _set_live_burn_in_db_equity(worktree, equity=100_000.0)
    try:
        info = capture_snapshot(worktree, pin_dir)
        snapshot_db = info.snapshot_root / "state" / "burn_in.db"
        assert snapshot_db.exists(), (
            "snapshot must inherit state/burn_in.db from the live worktree; "
            "got a snapshot without a state/ directory at all"
        )
        assert _read_portfolio_state_equity(snapshot_db) == 100_000.0, (
            "snapshot's state/burn_in.db must carry the live cohort's "
            "$100K equity so the pinned burner does not re-initialize "
            "to the generic $10K default"
        )
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


def test_pin_snapshot_inherits_live_market_data_cache(tmp_path: Path) -> None:
    """Regression: the snapshot's ``state/market_data_cache.db`` must
    copy the live cache at capture so the scanner starts from the same
    bar history, not an empty 8-row minimum."""
    from trading_bot.runtime.burnin_pin import capture_snapshot

    worktree = _make_worktree_isolated_repo(tmp_path)
    pin_dir = tmp_path / "burnin_pin"
    pin_dir.mkdir()
    _set_live_market_data_cache_contents(worktree, rows=42)
    try:
        info = capture_snapshot(worktree, pin_dir)
        snapshot_cache = info.snapshot_root / "state" / "market_data_cache.db"
        assert snapshot_cache.exists(), (
            "snapshot must inherit state/market_data_cache.db from the live worktree"
        )
        with sqlite3.connect(str(snapshot_cache)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        assert count == 42, (
            f"snapshot's market_data_cache must mirror the live cache row count "
            f"(live=42, snapshot={count})"
        )
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


def test_pin_snapshot_inherits_state_atomically_on_wal(tmp_path: Path) -> None:
    """While a writer holds an uncommitted transaction on the live DB,
    the snapshot must still capture a consistent point-in-time copy
    (SQLite online backup semantics). The snapshot must reflect the
    *committed* state, not the uncommitted writer's intermediate
    value, because the backup reads the committed state at the moment
    it acquires its SHARED lock.

    Note: SQLite's ``BEGIN IMMEDIATE`` takes a RESERVED lock, which
    is compatible with the SHARED lock that the backup acquires. The
    backup therefore does NOT block on a writer holding a transaction
    — it reads the committed state from the WAL/main-DB and ignores
    the writer's uncommitted changes. The writer's rollback simply
    discards those changes. This test pins contract: the snapshot's
    DB matches the committed (100K) state, not the uncommitted (999).
    """
    import threading
    from trading_bot.runtime.burnin_pin import capture_snapshot

    worktree = _make_worktree_isolated_repo(tmp_path)
    pin_dir = tmp_path / "burnin_pin"
    pin_dir.mkdir()
    _set_live_burn_in_db_equity(worktree, equity=100_000.0)
    live_db = worktree / "state" / "burn_in.db"
    started = threading.Event()
    release = threading.Event()
    snapshot_box: dict = {}

    def uncommitted_writer() -> None:
        writer = sqlite3.connect(str(live_db), timeout=30)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "UPDATE portfolio_state SET payload = ? WHERE id = 1",
                (json.dumps({
                    "cash": 999.0,
                    "equity": 999.0,
                    "positions": {},
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0,
                    "last_exited_at": {},
                }),),
            )
            started.set()
            release.wait(timeout=10)
            writer.rollback()
        finally:
            writer.close()

    try:
        writer_thread = threading.Thread(target=uncommitted_writer, daemon=True)
        writer_thread.start()
        assert started.wait(timeout=5), "writer thread did not start"

        # Capture snapshot WHILE the writer's uncommitted transaction
        # is in flight. The backup must produce a consistent image
        # that excludes the uncommitted writes.
        cap_thread = threading.Thread(
            target=lambda: snapshot_box.update(
                {"info": capture_snapshot(worktree, pin_dir)}
            ),
            daemon=True,
        )
        cap_thread.start()
        cap_thread.join(timeout=30)
        assert not cap_thread.is_alive(), (
            "backup must complete promptly even when a writer holds an "
            "uncommitted transaction"
        )
        release.set()
        writer_thread.join(timeout=5)

        info = snapshot_box["info"]
        snapshot_db = info.snapshot_root / "state" / "burn_in.db"
        snapshot_equity = _read_portfolio_state_equity(snapshot_db)
        assert snapshot_equity == 100_000.0, (
            f"snapshot must reflect committed state, not uncommitted writer "
            f"transaction (snapshot equity={snapshot_equity})"
        )
    finally:
        release.set()
        shutil.rmtree(worktree, ignore_errors=True)


def test_pin_snapshot_recaptures_inheritance_on_resnap(tmp_path: Path) -> None:
    """Recapturing with a mutated live DB must produce a snapshot that
    reflects the new state, not stale bytes from the first capture."""
    from trading_bot.runtime.burnin_pin import capture_snapshot

    worktree = _make_worktree_isolated_repo(tmp_path)
    pin_dir = tmp_path / "burnin_pin"
    pin_dir.mkdir()
    _set_live_burn_in_db_equity(worktree, equity=100_000.0)
    try:
        info1 = capture_snapshot(worktree, pin_dir)
        first_equity = _read_portfolio_state_equity(
            info1.snapshot_root / "state" / "burn_in.db"
        )
        assert first_equity == 100_000.0

        # Mutate the live DB and recapture.
        _set_live_burn_in_db_equity(worktree, equity=117_500.0)
        info2 = capture_snapshot(worktree, pin_dir)
        second_equity = _read_portfolio_state_equity(
            info2.snapshot_root / "state" / "burn_in.db"
        )
        assert second_equity == 117_500.0, (
            f"recapture must reflect the current live cohort, not the prior "
            f"snapshot's frozen bytes (got {second_equity})"
        )
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


def test_pin_snapshot_does_not_inherit_runtime_ephemera(tmp_path: Path) -> None:
    """Runtime ephemera (heartbeat, PID files, scan results) live
    under ``state/burn_in/`` and must NOT be inherited from the live
    worktree — the burner writes fresh ones on first cycle. Inheriting
    a stale heartbeat would falsely report the burner as healthy
    before it has actually run."""
    from trading_bot.runtime.burnin_pin import capture_snapshot

    worktree = _make_worktree_isolated_repo(tmp_path)
    pin_dir = tmp_path / "burnin_pin"
    pin_dir.mkdir()
    _seed_burn_in_ephemera(worktree)
    try:
        info = capture_snapshot(worktree, pin_dir)
        snapshot_burn_in = info.snapshot_root / "state" / "burn_in"
        for name in ("heartbeat.json", "burn_in.pid", "portfolio_summary.json"):
            candidate = snapshot_burn_in / name
            assert not candidate.exists(), (
                f"snapshot must NOT inherit runtime ephemera {name}; "
                f"the burner regenerates these on first cycle"
            )
    finally:
        _cleanup_burn_in_ephemera(worktree)
        shutil.rmtree(worktree, ignore_errors=True)


def test_pin_snapshot_inherited_burn_in_db_is_immutable_to_live_mutation(
    tmp_path: Path,
) -> None:
    """After inheritance, mutating the live DB must NOT change the
    snapshot's state/burn_in.db. The snapshot is a frozen capture;
    the immutability contract extends to inherited data, not just
    tracked code."""
    from trading_bot.runtime.burnin_pin import capture_snapshot

    worktree = _make_worktree_isolated_repo(tmp_path)
    pin_dir = tmp_path / "burnin_pin"
    pin_dir.mkdir()
    _set_live_burn_in_db_equity(worktree, equity=100_000.0)
    try:
        info = capture_snapshot(worktree, pin_dir)
        snapshot_db = info.snapshot_root / "state" / "burn_in.db"
        snapshot_sha = hashlib.sha256(snapshot_db.read_bytes()).hexdigest()
        snapshot_equity = _read_portfolio_state_equity(snapshot_db)
        assert snapshot_equity == 100_000.0

        # Mutate the live DB AFTER capture.
        _set_live_burn_in_db_equity(worktree, equity=123_456.0)

        # The snapshot's DB must NOT reflect the post-capture mutation.
        assert (
            hashlib.sha256(snapshot_db.read_bytes()).hexdigest()
            == snapshot_sha
        ), "snapshot's inherited state/burn_in.db must be immutable to post-capture mutations"
        assert _read_portfolio_state_equity(snapshot_db) == 100_000.0, (
            "snapshot's inherited state/burn_in.db must carry the captured "
            "equity, not the live one at read time"
        )
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


def test_pin_snapshot_inherit_state_disabled_by_env(tmp_path: Path) -> None:
    """``BURNIN_PIN_INHERIT_STATE=0`` / ``inherit_state=False`` must
    skip the inheritance step entirely — ad-hoc debugging should be
    able to capture a fresh-default snapshot without the live cohort
    polluting it."""
    from trading_bot.runtime.burnin_pin import capture_snapshot

    worktree = _make_worktree_isolated_repo(tmp_path)
    pin_dir = tmp_path / "burnin_pin"
    pin_dir.mkdir()
    _set_live_burn_in_db_equity(worktree, equity=100_000.0)
    try:
        info = capture_snapshot(worktree, pin_dir, inherit_state=False)
        snapshot_db = info.snapshot_root / "state" / "burn_in.db"
        assert not snapshot_db.exists(), (
            "with inherit_state=False the snapshot must NOT inherit "
            "state/burn_in.db; the legacy code-only snapshot is the "
            "documented override behavior"
        )
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


def test_pin_snapshot_inherited_db_has_no_wal_sidecars(tmp_path: Path) -> None:
    """After SQLite backup, the snapshot's state/burn_in.db must NOT
    carry ``-wal`` / ``-shm`` sidecars. The backup is already
    point-in-time consistent; carrying stale WAL could reapply
    pre-snapshot writes when the burner first opens the snapshot DB."""
    from trading_bot.runtime.burnin_pin import capture_snapshot

    worktree = _make_worktree_isolated_repo(tmp_path)
    pin_dir = tmp_path / "burnin_pin"
    pin_dir.mkdir()
    _set_live_burn_in_db_equity(worktree, equity=100_000.0)
    try:
        info = capture_snapshot(worktree, pin_dir)
        snapshot_db = info.snapshot_root / "state" / "burn_in.db"
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = snapshot_db.with_name(snapshot_db.name + suffix)
            assert not sidecar.exists(), (
                f"snapshot must not carry {sidecar.name} after inheritance; "
                f"the backup is already consistent"
            )
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


def test_wrapper_uses_pin_dir_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When PIN_DIR is set, the wrapper must prepend it to PYTHONPATH.

    The production path runs the wrapper as ``$PIN_DIR/tradebot-local``
    after the launcher captures the snapshot. ``ROOT_DIR`` then resolves
    to ``$PIN_DIR``, so PYTHONPATH gets the snapshot prepended and
    ``trading_bot`` imports from the snapshot, not the live worktree.

    We use ``capture_snapshot`` here so the snapshot is a real
    ``git archive`` extraction — the same byte-for-byte form the
    launcher relies on. A bare ``shutil.copy`` of the wrapper without
    a matching ``trading_bot`` package would fall through to the live
    install, which is a separate failure mode.
    """
    from trading_bot.runtime.burnin_pin import capture_snapshot

    pin_dir = _make_pin_dir(tmp_path)
    monkeypatch.setenv("PIN_DIR", str(pin_dir))

    snapshot = capture_snapshot(REPO_ROOT, pin_dir)
    pinned_wrapper = snapshot.wrapper_path

    # ``snapshot.python_executable`` is a symlink into the live
    # ``.venv`` by default so production runs work without copying
    # GB of packages. Tests cannot write through that symlink or
    # they will corrupt the live interpreter. Replace the symlink
    # with a regular directory and a stub script so we can drive
    # the wrapper without touching the live venv.
    snapshot_bin = snapshot.python_executable.parent
    snapshot_bin.mkdir(parents=True, exist_ok=True)
    if snapshot.python_executable.is_symlink() or snapshot.python_executable.exists():
        if snapshot.python_executable.is_symlink():
            snapshot.python_executable.unlink()
        elif snapshot.python_executable.is_file():
            snapshot.python_executable.unlink()
    stub_py = snapshot.python_executable
    stub_py.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, json\n"
        "out = {\n"
        "  'pythonpath': os.environ.get('PYTHONPATH', ''),\n"
        "  'argv': sys.argv[1:],\n"
        "}\n"
        "try:\n"
        "    import trading_bot\n"
        "    out['trading_bot_file'] = trading_bot.__file__\n"
        "except Exception as exc:\n"
        "    out['trading_bot_file_error'] = repr(exc)\n"
        "print(json.dumps(out))\n"
    )
    stub_py.chmod(0o755)

    try:
        result = subprocess.run(
            (str(pinned_wrapper), "doctor"),
            cwd=REPO_ROOT,
            env=os.environ,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"wrapper stderr: {result.stderr}"
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        # PYTHONPATH must include the snapshot root. Compare via
        # Path.resolve() on both sides so /var vs /private symlinks do
        # not produce a false negative on macOS.
        canonical_pythonpath = [
            Path(p).resolve() if p else Path("") for p in payload["pythonpath"].split(os.pathsep)
        ]
        assert snapshot.snapshot_root.resolve() in canonical_pythonpath, (
            f"PIN_DIR root {snapshot.snapshot_root} not prepended to "
            f"PYTHONPATH: {payload['pythonpath']!r}"
        )
        # trading_bot.__file__ must resolve under the pinned root.
        assert Path(payload["trading_bot_file"]).resolve().is_relative_to(
            snapshot.snapshot_root.resolve()
        ), (
            f"trading_bot resolved outside PIN_DIR: {payload['trading_bot_file']}"
        )
    finally:
        shutil.rmtree(snapshot.snapshot_root, ignore_errors=True)
        shutil.rmtree(pin_dir, ignore_errors=True)


def test_wrapper_falls_back_without_pin_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When PIN_DIR is unset, the wrapper must use the live worktree.

    We copy the wrapper into a temporary directory that has its own
    .venv stub so we can verify PYTHONPATH does NOT include the live
    repo root. The wrapper's logic is: when PIN_DIR is unset, ROOT_DIR
    is the wrapper's own directory, and PYTHONPATH is empty — not
    pointed at the live repo root.
    """
    monkeypatch.delenv("PIN_DIR", raising=False)

    fallback_root = tmp_path / "fallback_root"
    fallback_root.mkdir()
    (fallback_root / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(WRAPPER, fallback_root / "tradebot-local")
    fallback_wrapper = fallback_root / "tradebot-local"

    stub_py = fallback_root / ".venv" / "bin" / "python"
    stub_py.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, json\n"
        "out = {\n"
        "  'pythonpath': os.environ.get('PYTHONPATH', ''),\n"
        "  'argv': sys.argv[1:],\n"
        "}\n"
        "try:\n"
        "    import trading_bot\n"
        "    out['trading_bot_file'] = trading_bot.__file__\n"
        "except Exception as exc:\n"
        "    out['trading_bot_file_error'] = repr(exc)\n"
        "print(json.dumps(out))\n"
    )
    stub_py.chmod(0o755)

    try:
        result = subprocess.run(
            (str(fallback_wrapper), "doctor"),
            cwd=REPO_ROOT,
            env=os.environ,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"wrapper stderr: {result.stderr}"
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        # Without PIN_DIR, PYTHONPATH must not be augmented with the
        # wrapper's own directory — manual operators are unaffected.
        assert str(fallback_root) not in payload["pythonpath"].split(os.pathsep), (
            f"Without PIN_DIR, the wrapper must not prepended its own "
            f"dir to PYTHONPATH: {payload['pythonpath']!r}"
        )
    finally:
        shutil.rmtree(fallback_root, ignore_errors=True)
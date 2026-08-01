"""Burn-in runtime pin.

A live burn-in runs ``./tradebot-local`` for every scan, paper-trade,
manage-positions, drawdown check, and EOD watchdog fire. The
``scripts/auto-burn-in.sh`` shell remains resident with the revision
that was current at startup, but every Python subprocess re-imports
``trading_bot.main`` from the live mutable worktree.

The 2026-07-24 incident: while the burner was live, the operator
``git switch``ed to a legacy V2 marker branch. The next
``paper-trade`` subprocess re-imported the V2 ``circuit_breaker``,
read the legacy equity cohort, and halted at the exact 44.9296%
drawdown — while a fresh CLI on the same ledger reported 1.78%.

Fix: the operator launches the burner through ``scripts/burnin-launcher.sh``
which extracts HEAD into ``$PIN_DIR`` and exports ``PIN_DIR`` to all
child subprocesses. The wrapper ``tradebot-local`` prepends
``$PIN_DIR`` to ``PYTHONPATH``, so every subsequent import resolves
against the immutable snapshot — even if the operator switches
branches or dirty-edits tracked files underneath the live burner.

The ``capture_snapshot`` function below is the single source of truth
for that extraction. It runs ``git archive HEAD | tar -x -C <pin>/<sha>/``
which:

- preserves the exact committed state at the moment of capture;
- is byte-identical to a clean-checkout tarball smoke-test;
- does not touch the live worktree;
- records the SHA and a fingerprint of pinned paths.

The fingerprint is the SHA256 of the SHA plus the SHA256 of the
runtime-critical files (``tradebot-local``, ``scripts/auto-burn-in.sh``,
``scripts/start-dashboard.sh``, the configured YAML, and the snapshot's
own ``trading_bot`` package tree). Legitimate ``state/tuning_overrides.yaml``
mutations from a validated experiment do not change the fingerprint
because that file is excluded.

The 2026-07-30 cohort divergence: ``git archive HEAD`` excludes the
gitignored ``state/`` directory, so the snapshot's ``state/burn_in.db``
was freshly initialized to the generic $10K default on first cycle.
The pinned burner then operated against the wrong cohort while the
live worktree still held the $100K reset. ``capture_snapshot`` now
inherits the live ``state/burn_in.db`` and ``state/market_data_cache.db``
(via SQLite's online backup) plus ``state/tuning_experiments/`` so the
snapshot is a code + cohort capture. Runtime ephemera under
``state/burn_in/`` (heartbeat, PID files, scan results, portfolio
summary) are NOT inherited — the burner writes fresh ones on first
cycle. Disable inheritance with ``BURNIN_PIN_INHERIT_STATE=0`` for
ad-hoc debugging.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from trading_bot.db.permissions import secure_sqlite_artifacts

# Files whose content must be byte-stable for the pinned execution to
# mean what the operator thinks it means. Tracked Python under
# ``trading_bot/``, ``event_engine/``, ``scripts/``, ``ui/``, the
# wrapper, ``pyproject.toml``, and the active YAML config.
_DEFAULT_FINGERPRINT_GLOBS = (
    "tradebot-local",
    "pyproject.toml",
    "scripts/auto-burn-in.sh",
    "scripts/start-dashboard.sh",
    "burn-in-config.yaml",
    "config.yaml",
)

# State files copied from the live worktree into the snapshot at
# capture time so the pinned burner inherits the live cohort (2026-07-30).
# SQLite databases are copied via ``sqlite3.Connection.backup`` so the
# snapshot is a consistent point-in-time capture even when the live
# writer is mid-transaction. Non-DB files are copied via ``shutil.copy2``.
_INHERITED_STATE_FILES = (
    "state/burn_in.db",
    "state/market_data_cache.db",
)

# State directories copied recursively. ``state/tuning_experiments``
# carries the persisted experiment controller state and must travel
# with the cohort so a canary doesn't appear "new" on every snapshot.
_INHERITED_STATE_DIRS = (
    "state/tuning_experiments",
)

# WAL/SHM/JOURNAL sidecars that may travel with a SQLite file. The
# helper removes them from the snapshot after backup because the
# backup is already self-consistent — carrying a stale WAL would
# reapply pre-snapshot writes when the snapshot DB is first opened.
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclasses.dataclass(frozen=True)
class SnapshotInfo:
    """Result of ``capture_snapshot``."""

    repo_root: Path
    head_sha: str
    snapshot_root: Path
    pin_dir: Path
    fingerprint: str
    python_executable: Path
    wrapper_path: Path
    burner_script: Path


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(("git",) + args, cwd=cwd, text=True)


def _inherit_runtime_state(
    snapshot_root: Path,
    repo_root: Path,
    *,
    enabled: bool = True,
) -> tuple[int, int]:
    """Copy the live worktree's runtime state into the snapshot so
    the pinned burner inherits the cohort at capture time.

    Returns ``(copied, skipped)``. The caller emits a single
    ``inherit_state`` log line so the operator can see the effect.

    The helper honors ``BURNIN_PIN_INHERIT_STATE=0`` to disable
    inheritance for ad-hoc debugging — the legacy code-only snapshot
    is still available when the operator suspects the inherited data
    is the cause of an anomaly.

    SQLite files travel via ``sqlite3.Connection.backup``, which
    produces a consistent point-in-time copy even when the live
    writer is mid-transaction. WAL/SHM/JOURNAL sidecars are removed
    from the snapshot after the backup completes — the backup is
    already self-consistent, and carrying a stale WAL would reapply
    pre-snapshot writes when the snapshot DB is first opened.
    """
    if not enabled or os.environ.get("BURNIN_PIN_INHERIT_STATE") == "0":
        return (0, 0)

    copied = 0
    skipped = 0

    for relpath in _INHERITED_STATE_FILES:
        src = repo_root / relpath
        dst = snapshot_root / relpath
        if not src.exists():
            skipped += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Remove any pre-existing destination (refresh on recapture).
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            sidecar = dst.with_name(dst.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        # SQLite online backup acquires a SHARED lock on the source,
        # so it waits for any active writer to commit/rollback before
        # producing a consistent image. This is the canonical safe
        # way to copy a live SQLite DB. ``src.backup(dst)`` reads from
        # ``src`` and writes to ``dst``.
        src_conn = sqlite3.connect(str(src), timeout=30)
        try:
            dst_conn = sqlite3.connect(str(dst), timeout=30)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        # The backup is self-consistent; remove any WAL sidecars that
        # may have been carried alongside the source.
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            sidecar = dst.with_name(dst.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        secure_sqlite_artifacts(dst)
        copied += 1

    for relpath in _INHERITED_STATE_DIRS:
        src = repo_root / relpath
        dst = snapshot_root / relpath
        if not src.is_dir():
            skipped += 1
            continue
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, symlinks=False, ignore_dangling_symlinks=True)
        copied += 1

    return (copied, skipped)


def _fingerprint(snapshot_root: Path, head_sha: str) -> str:
    """Compute a stable fingerprint of pinned runtime content.

    The SHA alone is insufficient — dirty edits to a tracked file are
    not visible to ``git rev-parse HEAD``. We hash the SHA plus the
    content of every pinned-path glob so any uncommitted edit is
    visible in the heartbeat and the dashboard health response.
    """

    digest = hashlib.sha256()
    digest.update(head_sha.encode("utf-8"))
    for relpath in sorted(_DEFAULT_FINGERPRINT_GLOBS):
        candidate = snapshot_root / relpath
        if candidate.exists():
            digest.update(relpath.encode("utf-8"))
            digest.update(b"\0")
            digest.update(candidate.read_bytes())
    # Cover the application source tree by hashing the manifest of every
    # file under trading_bot/ and scripts/. This makes any dirty edit
    # visible without dragging the entire tarball into the digest.
    for base in ("trading_bot", "scripts", "event_engine", "ui"):
        root = snapshot_root / base
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                digest.update(str(path.relative_to(snapshot_root)).encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
    return digest.hexdigest()


def capture_snapshot(
    repo_root: Path,
    pin_dir: Path,
    *,
    fingerprint_globs: tuple[str, ...] = _DEFAULT_FINGERPRINT_GLOBS,
    inherit_state: bool = True,
) -> SnapshotInfo:
    """Extract ``HEAD`` of ``repo_root`` into ``pin_dir/<head_sha>/``.

    The extraction is byte-identical to ``git archive HEAD | tar -x`` and
    reuses the existing tarball smoke-test machinery. Subsequent
    subprocesses are pointed at the snapshot via the ``PIN_DIR``
    environment variable; see ``tradebot-local`` and
    ``scripts/auto-burn-in.sh``.

    After the tracked-tree extraction, the runtime state
    (``state/burn_in.db``, ``state/market_data_cache.db``, and
    ``state/tuning_experiments/`` if present) is copied from the live
    worktree into the snapshot so the pinned burner inherits the live
    cohort. Pass ``inherit_state=False`` (or set
    ``BURNIN_PIN_INHERIT_STATE=0``) to disable inheritance for ad-hoc
    debugging.

    Args:
        repo_root: Absolute path to the working tree.
        pin_dir: Parent directory that holds ``<pin_dir>/<sha>/``.
        fingerprint_globs: Tracked paths whose content is folded into
            the fingerprint.
        inherit_state: When ``True`` (the default), copy the live
            worktree's runtime state into the snapshot. Explicit
            ``False`` overrides the ``BURNIN_PIN_INHERIT_STATE`` env
            var.

    Returns:
        :class:`SnapshotInfo` with the resolved paths.

    Raises:
        RuntimeError: if ``git`` is missing, the tree is not a Git
            repo, or the snapshot cannot be extracted.
    """
    if shutil.which("git") is None:
        raise RuntimeError("git is required to capture a burn-in snapshot")
    if shutil.which("tar") is None:
        raise RuntimeError("tar is required to extract a burn-in snapshot")
    head_sha = _git("rev-parse", "HEAD", cwd=repo_root).strip()
    snapshot_root = pin_dir / head_sha
    pin_dir.mkdir(parents=True, exist_ok=True)
    if snapshot_root.exists():
        shutil.rmtree(snapshot_root)
    snapshot_root.mkdir(parents=True)

    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as archive:
        archive_path = Path(archive.name)
    try:
        subprocess.check_call(
            ("git", "archive", "HEAD", "-o", str(archive_path)),
            cwd=repo_root,
        )
        subprocess.check_call(
            ("tar", "-x", "-C", str(snapshot_root), "-f", str(archive_path)),
        )
    finally:
        archive_path.unlink(missing_ok=True)

    # Always create the .venv directory layout so the wrapper can
    # resolve ``$ROOT_DIR/.venv/bin/python``. By default we symlink
    # the live venv into the snapshot — the snapshot's source files
    # are the security boundary, not the interpreter. Tests opt out
    # of the symlink by clearing ``BURNIN_PIN_USE_LIVE_VENV``.
    src_venv = repo_root / ".venv"
    dst_venv = snapshot_root / ".venv"
    if src_venv.exists() and os.environ.get("BURNIN_PIN_USE_LIVE_VENV", "1") == "1":
        # Symlink the live venv into the snapshot. We use absolute
        # symlinks so the snapshot is portable across worktrees.
        if dst_venv.exists() or dst_venv.is_symlink():
            if dst_venv.is_symlink():
                dst_venv.unlink()
            else:
                shutil.rmtree(dst_venv)
        dst_venv.symlink_to(src_venv.resolve())
    else:
        # Stub directory so the wrapper's path check passes during
        # test extraction; the test stubs ``bin/python`` itself.
        (dst_venv / "bin").mkdir(parents=True, exist_ok=True)
        if src_venv.exists() and os.environ.get("BURNIN_PIN_COPY_VENV"):
            if dst_venv.exists():
                shutil.rmtree(dst_venv)
            shutil.copytree(src_venv, dst_venv, symlinks=True)

    # Inherit the live runtime state so the pinned burner sees the
    # cohort at capture time (2026-07-30 divergence fix). Disabled
    # via ``inherit_state=False`` or ``BURNIN_PIN_INHERIT_STATE=0``.
    copied, skipped = _inherit_runtime_state(
        snapshot_root, repo_root, enabled=inherit_state
    )
    # Emit to stderr so the launcher's stdout capture (which holds the
    # JSON payload for downstream parsing) is not polluted.
    print(
        f"inherit_state copied={copied} skipped={skipped}",
        file=sys.stderr,
    )

    # Recompute the fingerprint with the operator-provided globs so the
    # caller can extend it. The base implementation hashes a small set
    # of pinned paths plus the application source tree; we still hash
    # the head SHA for the additional globs to keep ordering stable.
    digest = hashlib.sha256()
    digest.update(head_sha.encode("utf-8"))
    for relpath in sorted(fingerprint_globs):
        candidate = snapshot_root / relpath
        if candidate.exists():
            digest.update(relpath.encode("utf-8"))
            digest.update(b"\0")
            digest.update(candidate.read_bytes())
    for base in ("trading_bot", "scripts", "event_engine", "ui"):
        root = snapshot_root / base
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                digest.update(str(path.relative_to(snapshot_root)).encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
    fingerprint = digest.hexdigest()

    return SnapshotInfo(
        repo_root=repo_root,
        head_sha=head_sha,
        snapshot_root=snapshot_root,
        pin_dir=pin_dir,
        fingerprint=fingerprint,
        python_executable=dst_venv / "bin" / "python",
        wrapper_path=snapshot_root / "tradebot-local",
        burner_script=snapshot_root / "scripts" / "auto-burn-in.sh",
    )


def resolve_pin_dir(env: os._Environ[str] | dict[str, str] | None = None) -> Path | None:
    """Return ``PIN_DIR`` from the environment, or None when unset.

    Accepts an explicit mapping for tests; defaults to ``os.environ``.
    """
    source = env if env is not None else os.environ
    value = source.get("PIN_DIR")
    if not value:
        return None
    return Path(value)


def resolve_tradebot_local(env: os._Environ[str] | dict[str, str] | None = None) -> Path | None:
    """Return the pinned wrapper path when ``PIN_DIR`` is set.

    Manual operators (without ``PIN_DIR``) keep using the live
    ``tradebot-local`` from the repo root.
    """
    pin_dir = resolve_pin_dir(env)
    if pin_dir is None:
        return None
    return pin_dir / "tradebot-local"

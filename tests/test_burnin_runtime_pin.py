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
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "scripts" / "burnin-launcher.sh"
WRAPPER = REPO_ROOT / "tradebot-local"
PIN_HELPER = REPO_ROOT / "trading_bot" / "runtime" / "burnin_pin.py"


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
    snapshot_auto_burn_sha = hashlib.sha256(snapshot_auto_burn.read_bytes()).hexdigest()
    snapshot_wrapper_sha = hashlib.sha256(snapshot_wrapper.read_bytes()).hexdigest()

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
    finally:
        # Restore the live worktree so the rest of the suite is unaffected.
        subprocess.check_call(("git", "checkout", "--", "scripts/auto-burn-in.sh", "tradebot-local"), cwd=REPO_ROOT)


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
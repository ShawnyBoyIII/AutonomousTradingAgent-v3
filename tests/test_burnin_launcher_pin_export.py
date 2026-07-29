"""Regression tests for the PIN_DIR handoff between launcher and auto-burn-in.

Root cause (2026-07-29 incident): ``scripts/burnin-launcher.sh`` exported
``PIN_DIR`` as the *parent* of the snapshot (``.burnin_pin/``), but
``scripts/auto-burn-in.sh`` resolved ``$PIN_DIR/tradebot-local`` against the
*snapshot root* (``.burnin_pin/<head_sha>/``). The
``[ -x "$PIN_DIR/tradebot-local" ]`` test always failed because the parent
directory has no wrapper — only ``<parent>/<head_sha>/tradebot-local`` does.
The burner therefore fell back to the relative ``./tradebot-local`` from a
hardcoded cwd in the live mutable worktree, silently defeating the pin and
exposing the running burner to ``git switch`` poison (the same class of
failure that caused the 2026-07-24 false drawdown halt).

These tests pin the handoff contract:

(a) Launcher dry-run exports an effective runtime PIN_DIR pointing at the
    snapshot root (i.e. ``<pin-parent>/<HEAD>``).
(b) ``<PIN_DIR>/tradebot-local`` and ``<PIN_DIR>/scripts/auto-burn-in.sh``
    exist after capture.
(c) ``auto-burn-in.sh``'s PIN_DIR block resolves PINNED_TRADEBOT /
    PINNED_PYTHON under the snapshot root when PIN_DIR is set.
(d) The manual fallback (PIN_DIR unset) still resolves
    ``./tradebot-local`` and ``./.venv/bin/python``, so manual operators
    are unaffected.

The live WRAPPER fallback (the wrapper's branch when PIN_DIR is unset
from a subprocess) is already covered by
``test_burnin_runtime_pin::test_wrapper_falls_back_without_pin_dir``;
we deliberately do not duplicate that test here so the existing contract
test stays untouched (the task forbids rewriting pin tests).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "scripts" / "burnin-launcher.sh"
AUTO_BURN_IN = REPO_ROOT / "scripts" / "auto-burn-in.sh"


def _git(*args: str) -> str:
    return subprocess.check_output(("git", *args), cwd=REPO_ROOT, text=True)


def _head_sha() -> str:
    return _git("rev-parse", "HEAD").strip()


def _run_launcher_dry_run(pin_parent: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PIN_DIR"] = str(pin_parent)
    env["PIN_DRY_RUN"] = "1"
    env["BURNIN_CONFIG"] = "config.yaml"
    return subprocess.run(
        ("/bin/bash", str(LAUNCHER)),
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _parse_effective_pin_dir(stdout: str) -> Path:
    """Parse the launcher's effective-runtime PIN_DIR emission from stdout."""
    # ``Effective runtime PIN_DIR: <path>`` or ``Effective runtime PIN_DIR=<path>``.
    # Tolerate either separator so the contract is the printed token, not its form.
    match = re.search(r"Effective runtime PIN_DIR[:=]\s*(\S+)", stdout)
    assert match, (
        "launcher dry-run must emit an 'Effective runtime PIN_DIR' line; "
        f"stdout was:\n{stdout}"
    )
    return Path(match.group(1)).resolve()


# --------------------------------------------------------------------- #
# (a) launcher dry-run exports effective runtime PIN_DIR=<pin-parent>/<HEAD>
# --------------------------------------------------------------------- #
def test_launcher_dry_run_exports_effective_pin_dir_to_snapshot_root(
    tmp_path: Path,
) -> None:
    pin_parent = tmp_path / "burnin_pin"
    pin_parent.mkdir()

    result = _run_launcher_dry_run(pin_parent)
    assert result.returncode == 0, f"launcher failed: {result.stderr}\n{result.stdout}"

    effective = _parse_effective_pin_dir(result.stdout)
    expected = (pin_parent / _head_sha()).resolve()
    assert effective == expected, (
        f"launcher must export effective runtime PIN_DIR pointing at the snapshot root; "
        f"expected {expected}, got {effective}. PIN_DIR pointing at the parent (e.g. "
        f"{pin_parent}) breaks auto-burn-in.sh's [ -x \"$PIN_DIR/tradebot-local\" ] "
        f"check and silently falls back to the live worktree."
    )


# --------------------------------------------------------------------- #
# (b) <PIN_DIR>/tradebot-local and <PIN_DIR>/scripts/auto-burn-in.sh exist
# --------------------------------------------------------------------- #
def test_pin_dir_paths_exist_after_launcher_dry_run(tmp_path: Path) -> None:
    pin_parent = tmp_path / "burnin_pin"
    pin_parent.mkdir()

    result = _run_launcher_dry_run(pin_parent)
    assert result.returncode == 0, f"launcher failed: {result.stderr}\n{result.stdout}"

    effective = _parse_effective_pin_dir(result.stdout)
    assert (effective / "tradebot-local").exists(), (
        f"{effective}/tradebot-local must exist after snapshot capture so the "
        f"pinned wrapper contract holds for child subprocesses"
    )
    assert (effective / "scripts" / "auto-burn-in.sh").exists(), (
        f"{effective}/scripts/auto-burn-in.sh must exist after snapshot capture "
        f"so auto-burn-in.sh's [ -x \"$PIN_DIR/tradebot-local\" ] check succeeds"
    )


# --------------------------------------------------------------------- #
# Helpers for (c) and (d): probe the PIN_DIR resolution block in isolation.
# The block mirrors lines 41-49 of scripts/auto-burn-in.sh exactly.
# --------------------------------------------------------------------- #
_AUTO_BURN_IN_RESOLUTION_BLOCK = """
if [ -n "${PIN_DIR:-}" ] && [ -x "$PIN_DIR/tradebot-local" ]; then
    PINNED_TRADEBOT="$PIN_DIR/tradebot-local"
    PINNED_PYTHON="$PIN_DIR/.venv/bin/python"
    PIN_RESOLVED=SNAPSHOT
else
    PINNED_TRADEBOT="./tradebot-local"
    PINNED_PYTHON="./.venv/bin/python"
    PIN_RESOLVED=MANUAL_FALLBACK
fi
echo "PINNED_TRADEBOT=$PINNED_TRADEBOT"
echo "PINNED_PYTHON=$PINNED_PYTHON"
echo "PIN_RESOLVED=$PIN_RESOLVED"
"""


def _run_resolution_block(env: dict[str, str], cwd: Path) -> dict[str, str]:
    result = subprocess.run(
        ("/bin/bash", "-c", _AUTO_BURN_IN_RESOLUTION_BLOCK),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"resolution probe failed: {result.stderr}"
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


# --------------------------------------------------------------------- #
# (c) auto-burn-in.sh resolves PINNED_TRADEBOT/PINNED_PYTHON under snapshot
#     root when PIN_DIR points at the snapshot, even when the parent also
#     has a ``tradebot-local`` (which is exactly the bug condition).
# --------------------------------------------------------------------- #
def test_auto_burn_in_resolves_pinned_paths_under_snapshot_root(
    tmp_path: Path,
) -> None:
    pin_parent = tmp_path / "burnin_pin"
    snapshot_root = pin_parent / _head_sha()
    snapshot_root.mkdir(parents=True)

    # Snapshot contents — these MUST be picked.
    snapshot_wrapper = snapshot_root / "tradebot-local"
    snapshot_wrapper.write_text("#!/bin/sh\necho snapshot-wrapper\n")
    snapshot_wrapper.chmod(0o755)
    snapshot_python = snapshot_root / ".venv" / "bin" / "python"
    snapshot_python.parent.mkdir(parents=True)
    snapshot_python.write_text("#!/bin/sh\necho snapshot-python\n")
    snapshot_python.chmod(0o755)

    # Parent ALSO has a wrapper — this is the bug condition. The bug had
    # PIN_DIR set to the parent, so this parent wrapper WOULD resolve if the
    # bug were unfixed. Adding it here forces the test to be specific to
    # the snapshot root, not the parent.
    parent_wrapper = pin_parent / "tradebot-local"
    parent_wrapper.write_text("#!/bin/sh\necho PARENT-wrapper-SHOULD-NOT-BE-PICKED\n")
    parent_wrapper.chmod(0o755)

    env = os.environ.copy()
    env["PIN_DIR"] = str(snapshot_root)

    out = _run_resolution_block(env, cwd=tmp_path)

    expected_wrapper = snapshot_wrapper.resolve()
    expected_python = snapshot_python.resolve()

    assert out.get("PIN_RESOLVED") == "SNAPSHOT", (
        f"expected PIN_RESOLVED=SNAPSHOT, got {out!r}; when PIN_DIR points at a "
        f"snapshot that contains the wrapper, the resolution must NOT fall back"
    )
    assert Path(out["PINNED_TRADEBOT"]).resolve() == expected_wrapper, (
        f"expected PINNED_TRADEBOT={expected_wrapper}, got {out['PINNED_TRADEBOT']}; "
        f"the SNAPSHOT wrapper must win, NOT {parent_wrapper} (the parent dir)"
    )
    assert Path(out["PINNED_PYTHON"]).resolve() == expected_python, (
        f"expected PINNED_PYTHON={expected_python}, got {out['PINNED_PYTHON']}"
    )


# --------------------------------------------------------------------- #
# (d) manual fallback (PIN_DIR unset) still resolves ./tradebot-local +
#     ./.venv/bin/python under cwd. Manual operators must be unaffected.
# --------------------------------------------------------------------- #
def test_auto_burn_in_falls_back_to_live_wrapper_when_pin_dir_unset() -> None:
    env = os.environ.copy()
    env.pop("PIN_DIR", None)

    out = _run_resolution_block(env, cwd=REPO_ROOT)

    expected_wrapper = (REPO_ROOT / "tradebot-local").resolve()
    expected_python = (REPO_ROOT / ".venv" / "bin" / "python").resolve()

    assert out.get("PIN_RESOLVED") == "MANUAL_FALLBACK", (
        f"expected PIN_RESOLVED=MANUAL_FALLBACK, got {out!r}"
    )
    assert Path(out["PINNED_TRADEBOT"]).resolve() == expected_wrapper, (
        f"expected PINNED_TRADEBOT={expected_wrapper}, got {out['PINNED_TRADEBOT']}"
    )
    assert Path(out["PINNED_PYTHON"]).resolve() == expected_python, (
        f"expected PINNED_PYTHON={expected_python}, got {out['PINNED_PYTHON']}"
    )


# --------------------------------------------------------------------- #
# Drift guard: production auto-burn-in.sh must still contain the resolution
# block we test. Without this, the helper script's snippet could silently
# drift away from the script's actual behavior — and the probe tests would
# pass while production was broken. This keeps the helper and the script
# locked together over time.
# --------------------------------------------------------------------- #
def test_auto_burn_in_pin_resolution_block_present() -> None:
    text = AUTO_BURN_IN.read_text()
    assert '[ -n "${PIN_DIR:-}" ]' in text
    assert '[ -x "$PIN_DIR/tradebot-local" ]' in text
    assert 'PINNED_TRADEBOT="$PIN_DIR/tradebot-local"' in text
    assert 'PINNED_PYTHON="$PIN_DIR/.venv/bin/python"' in text
    assert 'PINNED_TRADEBOT="./tradebot-local"' in text
    assert 'PINNED_PYTHON="./.venv/bin/python"' in text

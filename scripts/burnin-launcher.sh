#!/usr/bin/env bash
# Pin the burn-in to an immutable snapshot of the live worktree.
#
# The 2026-07-24 incident: a long-running burner kept re-importing the
# live ``trading_bot`` package while the operator ``git switch``ed to a
# legacy branch, producing a mixed-revision execution that falsely
# halted at 44.93% drawdown.
#
# This launcher captures ``HEAD`` into ``$PIN_PARENT_DIR/<head_sha>/``
# and execs ``auto-burn-in.sh`` from that snapshot. After capture it
# exports ``PIN_DIR`` pointing at the snapshot root, so every
# subsequent ``tradebot-local`` invocation resolves ``trading_bot``
# against ``$PIN_DIR`` via PYTHONPATH — a branch switch or dirty edit
# in the live worktree cannot affect the running burner.
#
# Usage:
#   ./scripts/burnin-launcher.sh [args passed to auto-burn-in.sh]
#
# Environment overrides:
#   BURNIN_CONFIG       - config path passed to the burner (default: burn-in-config.yaml)
#   PIN_DIR             - parent directory holding per-head snapshots (default: $REPO/.burnin_pin)
#   PIN_PARENT_DIR      - explicit override for the snapshot parent directory
#   PIN_DRY_RUN         - if set, capture the snapshot and print info but do not exec the burner
#   BURNIN_FINGERPRINT_OUT - file to write the snapshot fingerprint (default: $PIN_PARENT_DIR/last_fingerprint)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

BURNIN_CONFIG="${BURNIN_CONFIG:-burn-in-config.yaml}"
# PIN_PARENT_DIR holds one snapshot subdir per capture
# (``<PIN_PARENT_DIR>/<head_sha>/``). The launcher exports PIN_DIR
# pointing at that snapshot root after capture so child subprocesses
# see the pinned wrapper and python, NOT the parent (which carries no
# executable and would defeat the pin — see the 2026-07-29 dry-run
# fallback bug in this file's history).
PIN_PARENT_DIR="${PIN_PARENT_DIR:-${PIN_DIR:-$REPO_ROOT/.burnin_pin}}"
BURNIN_FINGERPRINT_OUT="${BURNIN_FINGERPRINT_OUT:-$PIN_PARENT_DIR/last_fingerprint}"

if ! command -v git >/dev/null 2>&1; then
  echo "error: git is required for the burn-in pin" >&2
  exit 1
fi
if ! command -v tar >/dev/null 2>&1; then
  echo "error: tar is required for the burn-in pin" >&2
  exit 1
fi

mkdir -p "$PIN_PARENT_DIR"

# Capture the snapshot via the Python helper so the launcher, the
# dashboard launcher, and the doctor can share the same extraction
# logic. ``git archive HEAD | tar -x -C <pin>/<sha>/`` is the canonical
# immutable form. The Python sees REPO_ROOT and PIN_PARENT_DIR via
# environment so the heredoc does not have to interpolate them.
export BURNIN_LAUNCHER_REPO_ROOT="$REPO_ROOT"
export BURNIN_LAUNCHER_PIN_DIR="$PIN_PARENT_DIR"
PIN_INFO_JSON="$(.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
from trading_bot.runtime.burnin_pin import capture_snapshot
info = capture_snapshot(
    Path(os.environ["BURNIN_LAUNCHER_REPO_ROOT"]),
    Path(os.environ["BURNIN_LAUNCHER_PIN_DIR"]),
)
print(json.dumps({
    "head_sha": info.head_sha,
    "snapshot_root": str(info.snapshot_root),
    "fingerprint": info.fingerprint,
    "python_executable": str(info.python_executable),
    "wrapper_path": str(info.wrapper_path),
    "burner_script": str(info.burner_script),
}))
PY
)"
HEAD_SHA="$(.venv/bin/python -c 'import json,sys; print(json.loads(sys.stdin.read())["head_sha"])' <<<"$PIN_INFO_JSON")"
SNAPSHOT_ROOT="$(.venv/bin/python -c 'import json,sys; print(json.loads(sys.stdin.read())["snapshot_root"])' <<<"$PIN_INFO_JSON")"
FINGERPRINT="$(.venv/bin/python -c 'import json,sys; print(json.loads(sys.stdin.read())["fingerprint"])' <<<"$PIN_INFO_JSON")"

echo "=========================================="
echo "Burn-in pinned to snapshot"
echo "  HEAD:      $HEAD_SHA"
echo "  Snapshot:  $SNAPSHOT_ROOT"
echo "  Fingerprint: ${FINGERPRINT:0:16}…"
echo "=========================================="

printf '%s\n' "$FINGERPRINT" >"$BURNIN_FINGERPRINT_OUT"

# After capture: repoint PIN_DIR at the snapshot root. Children
# resolve ``$PIN_DIR/tradebot-local`` and ``$PIN_DIR/.venv/bin/python``
# against ``$SNAPSHOT_ROOT``, so they pick up the immutable snapshot
# rather than the mutable parent. ``PIN_PARENT_DIR`` stays exported so
# the doctor and dashboard sidecars can locate the fingerprint file
# under the parent directory.
PIN_DIR="$SNAPSHOT_ROOT"
export PIN_DIR
export PIN_PARENT_DIR
export BURNIN_FINGERPRINT_OUT
export HEAD_SHA

if [ -n "${PIN_DRY_RUN:-}" ]; then
  echo "PIN_DRY_RUN set; not execing auto-burn-in.sh"
  echo "  Effective runtime PIN_DIR: $PIN_DIR"
  exit 0
fi

# Exec the burner from the snapshot. Every subprocess it spawns
# inherits PIN_DIR, so ``tradebot-local`` will resolve ``trading_bot``
# against $SNAPSHOT_ROOT instead of the live worktree.
exec "$SNAPSHOT_ROOT/scripts/auto-burn-in.sh" "$@"
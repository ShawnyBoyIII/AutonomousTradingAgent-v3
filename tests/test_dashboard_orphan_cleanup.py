"""Tests for the dashboard orphan-detection logic (2026-07-09 fix).

2026-07-09 incident: a previous session's uvicorn process was still
alive in the process table but had lost its listening socket.  When
``auto-burn-in.sh`` tried to start a new dashboard, ``start-dashboard.sh``
saw the port still bound and exited with "port already in use",
silently failing.

The fix in ``ensure_dashboard``: before starting, check if there's
something on the port; if so, do a 1-second /api/health probe.  If
the probe fails, treat it as an orphan and SIGKILL the holder before
proceeding.  If the probe succeeds, skip — another dashboard is
already serving.
"""

from __future__ import annotations

import re
from pathlib import Path


_SCRIPT = Path(__file__).parent.parent / "scripts" / "auto-burn-in.sh"


def test_ensure_dashboard_contains_orphan_cleanup() -> None:
    """ensure_dashboard() must include a /api/health probe for orphan detection."""
    content = _SCRIPT.read_text(encoding="utf-8")
    # Find ensure_dashboard function body
    start = content.find("ensure_dashboard() {")
    assert start > 0
    end = content.find("\n}\n", start)
    body = content[start:end]
    assert "/api/health" in body, "ensure_dashboard must probe /api/health"
    assert "lsof -ti" in body, "ensure_dashboard must use lsof to detect port holder"
    assert "kill -9" in body, "ensure_dashboard must kill orphan port holders"


def test_ensure_dashboard_logs_orphan_clear() -> None:
    """When an orphan is found and cleared, the user-visible log line is present."""
    content = _SCRIPT.read_text(encoding="utf-8")
    # Look for the human-readable log line
    assert "Found orphan on port" in content
    assert "clearing" in content


def test_ensure_dashboard_skips_if_healthy() -> None:
    """When a healthy dashboard already serves the port, ensure_dashboard skips."""
    content = _SCRIPT.read_text(encoding="utf-8")
    start = content.find("ensure_dashboard() {")
    end = content.find("\n}\n", start)
    body = content[start:end]
    assert "Port $DASHBOARD_PORT already serves" in body
    assert "skipping sidecar" in body


def test_orphan_cleanup_uses_one_second_timeout() -> None:
    """The /api/health probe uses a 1-second timeout (fast health check)."""
    content = _SCRIPT.read_text(encoding="utf-8")
    # curl -m 1 means 1-second timeout
    assert re.search(r"curl.*-m\s+1.*api/health", content) is not None, (
        "Health probe should use a 1-second timeout (curl -m 1)"
    )


def test_orphan_cleanup_uses_sigkill() -> None:
    """Orphan cleanup uses SIGKILL (kill -9) — the orphan is already unresponsive."""
    content = _SCRIPT.read_text(encoding="utf-8")
    start = content.find("ensure_dashboard() {")
    end = content.find("\n}\n", start)
    body = content[start:end]
    assert "xargs kill -9" in body

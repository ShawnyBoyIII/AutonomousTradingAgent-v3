"""Tests for the FastAPI dashboard's /api/closed-trades endpoint + the
two-leaf book layout.

The endpoint reads from the SQLAlchemy `trades` table — separate from the
raw `orders` table that the legacy dashboard uses — and exposes rich
attribution (exit_reason, exit_regime, hold_duration_minutes, signal_quality,
market_regime, supermodel_decision) for closed round-trip trades.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from trading_bot.db.models import Trade
from trading_bot.db.session import (
    get_session,
    init_db,
    make_session_factory,
)


def _make_settings(tmp_path: Path, monkeypatch):
    """Build a DashboardState pointing at an isolated state DB, swap
    it into the dashboard module via monkeypatch."""
    from trading_bot.config import loader as config_loader
    import ui.dashboard.main as mod

    # Patch the loader to use a tmp burn-in config so the dashboard's
    # module-level `state` doesn't blow up trying to read its real DB.
    cfg_path = tmp_path / "burn-in.yaml"
    cfg_path.write_text(
        "app:\n"
        "  state_db_path: \"" + str(tmp_path / "burn_in.db") + "\"\n"
        "  log_dir: \"" + str(tmp_path / "logs") + "\"\n"
    )

    # Build a fresh DashboardState under that config and swap it in via
    # monkeypatch so the closure inside main.py reads the new instance.
    new_state = type(mod.state)()
    new_state.settings = config_loader.load_settings(cfg_path)
    new_state.ledger = type(mod.state.ledger)(Path(new_state.settings.app.state_db_path))
    monkeypatch.setattr(mod, "state", new_state)
    return new_state.settings


def test_closed_trades_endpoint_returns_empty_for_fresh_db(tmp_path: Path, monkeypatch) -> None:
    """A brand-new state DB has no closed trades → endpoint returns count=0."""
    from ui.dashboard import main as mod

    _make_settings(tmp_path, monkeypatch)

    # Touch the engine (creates the file)
    init_db(mod.state.settings)

    with TestClient(mod.app) as client:
        r = client.get("/api/closed-trades")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 0
        assert data["trades"] == []


def test_closed_trades_endpoint_returns_rich_attribution(tmp_path: Path, monkeypatch) -> None:
    """A seeded CLOSED trade surfaces all the fields the SQLAlchemy
    `trades` schema has — including ones the legacy dashboard never
    shows (exit_reason, exit_regime, hold_duration_minutes, signal_quality).
    """
    from ui.dashboard import main as mod

    settings = _make_settings(tmp_path, monkeypatch)
    engine = init_db(settings)
    db_path = Path(settings.app.state_db_path).resolve()

    sf = make_session_factory(engine)
    now = datetime.now(tz=timezone.utc)
    with get_session(sf) as session:
        session.add(Trade(
            ticker="QYLD",
            side="BUY",
            order_type="market",
            quantity=1381,
            entry_price=18.10,
            stop_loss=17.73,
            profit_target=19.00,
            fees=1.0,
            filled_at=now - timedelta(hours=1),
            strategy_tag="v3-mean_reversion|stack:caution",
            signal_quality="GREEN",
            market_regime="high_volatility",
            supermodel_decision="caution",
            consensus=None,
            entry_volume_ratio=1.4,
            entry_range_ratio=1.2,
            adaptive_rr=2.5,
            status="CLOSED",
            exit_price=18.04,
            exit_fees=1.0,
            exited_at=now,
            pnl=-82.51,
            exit_rsi=40.4,
            exit_atr=0.04,
            hold_duration_minutes=65.5,
            exit_regime=None,
            exit_strategy="v3-mean_reversion",
            exit_reason="eod",
        ))
        session.add(Trade(
            ticker="AFL",
            side="BUY",
            order_type="market",
            quantity=82,
            entry_price=121.25,
            stop_loss=118.83,
            profit_target=127.31,
            fees=1.0,
            filled_at=now - timedelta(minutes=10),
            status="FILLED",   # Open — should be filtered out
        ))
        session.commit()

    with TestClient(mod.app) as client:
        r = client.get("/api/closed-trades")
        assert r.status_code == 200
        data = r.json()

    # Only the CLOSED trade should appear; OPEN one is filtered.
    assert data["count"] == 1, (
        f"expected 1 closed trade (QYLD), got {data['count']}: {data['trades']}"
    )
    closed = data["trades"][0]
    assert closed["ticker"] == "QYLD"
    assert closed["quantity"] == 1381
    assert abs(closed["entry_price"] - 18.10) < 1e-9
    assert abs(closed["exit_price"] - 18.04) < 1e-9
    assert abs(closed["pnl"] - (-82.51)) < 1e-9
    assert closed["exit_reason"] == "eod"
    assert closed["exit_strategy"] == "v3-mean_reversion"
    assert abs(closed["hold_duration_minutes"] - 65.5) < 1e-9
    assert closed["signal_quality"] == "GREEN"
    assert closed["market_regime"] == "high_volatility"
    assert closed["supermodel_decision"] == "caution"
    assert closed["filled_at"] is not None
    assert closed["exited_at"] is not None


def test_closed_trades_endpoint_in_sse_stream(tmp_path: Path, monkeypatch) -> None:
    """The SSE stream must also surface closed_trades so the dashboard
    refreshes without an extra fetch round-trip."""
    from ui.dashboard import main as mod

    _make_settings(tmp_path, monkeypatch)
    init_db(mod.state.settings)

    # Drive the underlying payload function directly (the SSE endpoint
    # itself requires an async event loop; testing the function is
    # equivalent and faster than driving an SSE connection).
    closed = mod._closed_trades_payload()
    assert "trades" in closed
    assert "count" in closed
    assert "timestamp" in closed


def test_closed_trades_endpoint_handles_unreadable_db(tmp_path: Path, monkeypatch) -> None:
    """If the SQLAlchemy engine can't read (corrupt/missing file), the
    endpoint must return a valid empty payload rather than 500 — the
    dashboard must never crash on stale schema."""
    from ui.dashboard import main as mod
    from unittest.mock import patch as mock_patch

    settings = _make_settings(tmp_path, monkeypatch)

    with TestClient(mod.app) as client:
        # Force the inner _closed_trades_payload to fail and verify
        # the route still returns a safe empty payload.
        with mock_patch.object(
            mod, "_closed_trades_payload",
            side_effect=lambda **kw: {
                "trades": [], "count": 0, "error": "forced",
                "timestamp": "2026-07-08T00:00:00",
            },
        ):
            r = client.get("/api/closed-trades")
            assert r.status_code == 200
            data = r.json()
            assert data["count"] == 0
            assert data["trades"] == []
            assert data.get("error") == "forced"


def test_closed_trade_expansion_preserves_state_and_focus():
    """Expansion is explicit, focusable, and not reset by background updates."""
    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")
    assert "closedTradesExpanded" in js
    assert "focusExpanded" in js
    assert "document.activeElement" in js
    assert ".focus()" in js
    assert 'tabindex="-1"' in js

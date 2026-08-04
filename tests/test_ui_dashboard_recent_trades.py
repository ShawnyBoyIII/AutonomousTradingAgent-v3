from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from trading_bot.models.order import FillResult
from trading_bot.portfolio.ledger import PortfolioLedger


def _fill(
    order_id: str,
    ticker: str,
    filled_at: datetime,
    *,
    quantity: int,
    price: float,
    fees: float,
) -> FillResult:
    return FillResult(
        order_id=order_id,
        ticker=ticker,
        quantity=quantity,
        fill_price=price,
        fees=fees,
        filled_at=filled_at,
    )


def _set_dashboard_state(monkeypatch, tmp_path: Path) -> PortfolioLedger:
    from ui.dashboard import main

    ledger = PortfolioLedger(tmp_path / "dashboard.db")
    settings = SimpleNamespace(
        app=SimpleNamespace(
            log_dir=tmp_path / "logs",
            timezone="America/New_York",
        )
    )
    monkeypatch.setattr(main, "state", SimpleNamespace(ledger=ledger, settings=settings))
    return ledger


def test_trades_payload_reads_persisted_buy_and_sell_without_decision_log(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from ui.dashboard import main

    ledger = _set_dashboard_state(monkeypatch, tmp_path)
    ledger.record_fill(
        _fill(
            "buy-1",
            "AAPL",
            datetime(2026, 7, 22, 13, 30, tzinfo=timezone.utc),
            quantity=10,
            price=201.25,
            fees=0.25,
        ),
        side="BUY",
        strategy_tag="v3-trend",
    )
    ledger.record_fill(
        _fill(
            "sell-1",
            "AAPL",
            datetime(2026, 7, 22, 14, 30, tzinfo=timezone.utc),
            quantity=4,
            price=204.5,
            fees=0.15,
        ),
        side="SELL",
        realized_pnl=12.85,
        strategy_tag="v3-trend",
    )

    payload = main._trades_payload()

    assert payload["count"] == 2
    assert payload["trades"] == [
        {
            "timestamp": "2026-07-22T14:30:00+00:00",
            "type": "FILL",
            "symbol": "AAPL",
            "side": "SELL",
            "quantity": 4,
            "price": 204.5,
            "fees": 0.15,
            "pnl": 12.85,
            "strategy_tag": "v3-trend",
            "order_id": "sell-1",
        },
        {
            "timestamp": "2026-07-22T13:30:00+00:00",
            "type": "FILL",
            "symbol": "AAPL",
            "side": "BUY",
            "quantity": 10,
            "price": 201.25,
            "fees": 0.25,
            "pnl": 0.0,
            "strategy_tag": "v3-trend",
            "order_id": "buy-1",
        },
    ]


def test_trades_payload_limit_and_decision_log_contents_have_no_effect(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from ui.dashboard import main

    ledger = _set_dashboard_state(monkeypatch, tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "decision-log.jsonl").write_text(
        "not-json\n" + json.dumps({"event_type": "FILL", "symbol": "FAKE"}),
        encoding="utf-8",
    )
    (log_dir / "decision-log.jsonl.1").write_text(
        json.dumps({"event_type": "FILL", "symbol": "ROTATED"}),
        encoding="utf-8",
    )
    for index in range(3):
        ledger.record_fill(
            _fill(
                f"fill-{index}",
                f"SYM{index}",
                datetime(2026, 7, 22, 13, index, tzinfo=timezone.utc),
                quantity=index + 1,
                price=100.0 + index,
                fees=0.0,
            ),
            side="BUY",
        )

    payload = main._trades_payload(limit=2)

    assert [trade["order_id"] for trade in payload["trades"]] == ["fill-2", "fill-1"]
    assert {trade["symbol"] for trade in payload["trades"]}.isdisjoint({"FAKE", "ROTATED"})


def test_trades_payload_returns_empty_for_fresh_ledger(monkeypatch, tmp_path: Path) -> None:
    from ui.dashboard import main

    _set_dashboard_state(monkeypatch, tmp_path)

    payload = main._trades_payload()

    assert payload["trades"] == []
    assert payload["count"] == 0


def test_timestamp_tooltips_are_keyboard_accessible():
    """Exact timestamps are reachable and named for keyboard users."""
    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")
    assert 'class="alert__time"' in js
    assert 'class="trade__time"' in js
    assert 'tabindex="0"' in js
    assert 'aria-label="Exact time:' in js


def test_trades_payload_logs_and_isolates_ledger_read_failure(
    monkeypatch,
    caplog,
) -> None:
    from ui.dashboard import main

    class BrokenLedger:
        def list_recent_order_rows(self, limit: int, naive_timezone: str | None = None):
            raise OSError("unreadable ledger")

    settings = SimpleNamespace(app=SimpleNamespace(timezone="America/New_York"))
    monkeypatch.setattr(
        main,
        "state",
        SimpleNamespace(ledger=BrokenLedger(), settings=settings),
    )

    with caplog.at_level("WARNING"):
        payload = main._trades_payload()

    assert payload["trades"] == []
    assert payload["count"] == 0
    assert "Could not read recent fills" in caplog.text


def test_trades_payload_passes_configured_timezone_to_ledger(monkeypatch) -> None:
    from ui.dashboard import main

    calls = []

    class CapturingLedger:
        def list_recent_order_rows(
            self,
            limit: int,
            naive_timezone: str | None = None,
        ):
            calls.append((limit, naive_timezone))
            return []

    settings = SimpleNamespace(app=SimpleNamespace(timezone="America/New_York"))
    monkeypatch.setattr(
        main,
        "state",
        SimpleNamespace(ledger=CapturingLedger(), settings=settings),
    )

    main._trades_payload(limit=7)

    assert calls == [(7, "America/New_York")]


def test_trades_endpoint_and_sse_use_shared_payload_owner(monkeypatch) -> None:
    from ui.dashboard import main

    expected = {"trades": [{"order_id": "shared"}], "count": 1, "timestamp": "now"}
    calls = []

    def shared_payload(limit: int = 20):
        calls.append(limit)
        return expected

    monkeypatch.setattr(main, "_trades_payload", shared_payload)

    with TestClient(main.app) as client:
        response = client.get("/api/trades?limit=7")
    assert response.json() == expected

    monkeypatch.setattr(main, "_portfolio_payload", lambda: {})
    monkeypatch.setattr(main, "_health_payload", lambda: {})
    monkeypatch.setattr(main, "_alerts_payload", lambda: {})
    monkeypatch.setattr(main, "_closed_trades_payload", lambda: {})
    monkeypatch.setattr(main, "_evaluation_windows_payload", lambda: {})

    async def read_one_event() -> dict:
        generator = main.event_generator()
        event = await anext(generator)
        await generator.aclose()
        return json.loads(event.removeprefix("data: ").strip())

    event = asyncio.run(read_one_event())

    assert event["trades"] == expected
    assert calls == [7, 20]

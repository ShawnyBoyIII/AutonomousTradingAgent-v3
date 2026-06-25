import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.brokers.robinhood.boundary import RobinhoodBrokerBoundary
from trading_bot.config.loader import load_settings


def _write_snapshot_bundle(state_dir: Path, *, fresh: bool = True) -> None:
    synced_at = datetime(2026, 6, 19, 10, 0, 0)
    fresh_until = synced_at + timedelta(minutes=15) if fresh else synced_at - timedelta(minutes=1)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "robinhood_sync_meta.json").write_text(
        json.dumps(
            {
                "source": "mcp",
                "account_number": "ACC123",
                "synced_at": synced_at.isoformat(),
                "fresh_until": fresh_until.isoformat(),
                "capabilities": {
                    "read_only": True,
                    "shadow_preview": True,
                    "live_submit": False,
                    "live_cancel": False,
                },
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "robinhood_account.json").write_text(
        json.dumps(
            {
                "account_number": "ACC123",
                "cash": 1200.5,
                "equity": 2500.0,
                "buying_power": 1800.0,
                "updated_at": synced_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "robinhood_positions.json").write_text(
        json.dumps(
            [
                {
                    "symbol": "AAPL",
                    "quantity": 5,
                    "average_cost": 100.0,
                    "current_price": 110.0,
                    "market_value": 550.0,
                }
            ]
        ),
        encoding="utf-8",
    )
    (state_dir / "robinhood_orders.json").write_text(
        json.dumps(
            [
                {
                    "order_id": "ord-1",
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": 5,
                    "state": "filled",
                    "type": "limit",
                    "created_at": synced_at.isoformat(),
                }
            ]
        ),
        encoding="utf-8",
    )


def _load_boundary(tmp_path: Path) -> RobinhoodBrokerBoundary:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "robinhood:\n"
        "  enabled: true\n",
        encoding="utf-8",
    )
    settings = load_settings(config_file)
    return RobinhoodBrokerBoundary(settings, now_fn=lambda: datetime(2026, 6, 19, 10, 5, 0))


def test_boundary_reports_disconnected_when_sync_snapshots_are_missing(tmp_path: Path) -> None:
    boundary = _load_boundary(tmp_path)

    status = boundary.get_status()

    assert status.connected is False
    assert status.source == "disabled" or status.source == "mcp"
    assert "sync" in status.reason.lower()


def test_boundary_loads_valid_snapshots(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_snapshot_bundle(state_dir, fresh=True)
    boundary = _load_boundary(tmp_path)

    status = boundary.get_status()
    account = boundary.get_portfolio("ACC123")
    positions = boundary.get_positions_for("ACC123")
    orders = boundary.get_orders_for("ACC123")

    assert status.connected is True
    assert status.source == "mcp"
    assert status.capabilities.read_only is True
    assert account is not None
    assert account.account_number == "ACC123"
    assert positions[0].symbol == "AAPL"
    assert orders[0].order_id == "ord-1"
    assert orders[0].order_type == "limit"


def test_boundary_blocks_live_submit_intent_when_sync_is_stale(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_snapshot_bundle(state_dir, fresh=False)
    boundary = _load_boundary(tmp_path)

    result = boundary.submit_live_order_intent(
        account_number="ACC123",
        symbol="AAPL",
        side="buy",
        quantity=5,
        order_type="market",
    )

    assert result.accepted is False
    assert "stale" in result.reason.lower()


def test_boundary_logs_shadow_preview_intent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_snapshot_bundle(state_dir, fresh=True)
    boundary = _load_boundary(tmp_path)

    result = boundary.preview_shadow_order(
        account_number="ACC123",
        symbol="AAPL",
        side="buy",
        quantity=5,
        order_type="market",
    )

    assert result.accepted is True
    assert result.intent.mcp_action == "review_equity_order"
    log_path = state_dir / "robinhood_intents.jsonl"
    assert log_path.exists()
    assert "AAPL" in log_path.read_text(encoding="utf-8")


def test_boundary_normalizes_order_types_and_states(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_snapshot_bundle(state_dir, fresh=True)
    (state_dir / "robinhood_orders.json").write_text(
        json.dumps(
            [
                {
                    "order_id": "ord-2",
                    "symbol": "MSFT",
                    "side": "sell",
                    "quantity": 2,
                    "state": "partially_filled",
                    "type": "stop_limit",
                    "created_at": datetime(2026, 6, 19, 10, 0, 0).isoformat(),
                },
                {
                    "order_id": "ord-3",
                    "symbol": "TSLA",
                    "side": "buy",
                    "quantity": 1,
                    "state": "voided",
                    "type": "market",
                    "created_at": datetime(2026, 6, 19, 10, 1, 0).isoformat(),
                },
            ]
        ),
        encoding="utf-8",
    )
    boundary = _load_boundary(tmp_path)

    orders = boundary.get_orders()

    assert orders[0].order_type.name == "STOP_LIMIT"
    assert orders[0].status.name == "PARTIAL"
    assert orders[1].status.name == "REJECTED"

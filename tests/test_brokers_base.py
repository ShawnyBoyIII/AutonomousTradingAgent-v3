"""Tests for broker adapter base module (271 lines)."""

from __future__ import annotations

from abc import ABC
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from trading_bot.brokers.base import (
    BrokerAccount,
    BrokerAdapter,
    BrokerMode,
    BrokerOrder,
    BrokerPosition,
    OrderPreview,
    OrderSide,
    OrderStatus,
    OrderType,
)


class TestBrokerMode:
    def test_paper_mode(self):
        assert BrokerMode.PAPER.value is not None

    def test_shadow_mode(self):
        assert BrokerMode.SHADOW.value is not None

    def test_live_mode(self):
        assert BrokerMode.LIVE.value is not None


class TestOrderStatus:
    def test_pending(self):
        assert OrderStatus.PENDING.value is not None

    def test_filled(self):
        assert OrderStatus.FILLED.value is not None

    def test_partial(self):
        assert OrderStatus.PARTIAL.value is not None

    def test_rejected(self):
        assert OrderStatus.REJECTED.value is not None

    def test_cancelled(self):
        assert OrderStatus.CANCELLED.value is not None


class TestOrderSide:
    def test_buy(self):
        assert OrderSide.BUY.value == "BUY"

    def test_sell(self):
        assert OrderSide.SELL.value == "SELL"


class TestOrderType:
    def test_market(self):
        assert OrderType.MARKET.value == "MARKET"

    def test_limit(self):
        assert OrderType.LIMIT.value == "LIMIT"

    def test_stop(self):
        assert OrderType.STOP.value == "STOP"

    def test_stop_limit(self):
        assert OrderType.STOP_LIMIT.value == "STOP_LIMIT"


class TestBrokerAccount:
    def test_basic_account(self):
        account = BrokerAccount(
            account_id="test_001",
            cash=Decimal("10000.00"),
            equity=Decimal("12000.00"),
            buying_power=Decimal("20000.00"),
        )
        assert account.account_id == "test_001"
        assert account.cash == Decimal("10000.00")
        assert account.equity == Decimal("12000.00")
        assert account.currency == "USD"

    def test_account_with_timestamp(self):
        ts = datetime(2026, 6, 20, 10, 0, 0)
        account = BrokerAccount(
            account_id="test_001",
            cash=Decimal("10000.00"),
            equity=Decimal("12000.00"),
            buying_power=Decimal("20000.00"),
            timestamp=ts,
        )
        assert account.timestamp == ts

    def test_account_to_dict(self):
        ts = datetime(2026, 6, 20, 10, 0, 0)
        account = BrokerAccount(
            account_id="test_001",
            cash=Decimal("10000.00"),
            equity=Decimal("12000.00"),
            buying_power=Decimal("20000.00"),
            currency="USD",
            timestamp=ts,
        )
        d = account.to_dict()
        assert d["account_id"] == "test_001"
        assert d["cash"] == 10000.0
        assert d["equity"] == 12000.0
        assert d["currency"] == "USD"
        assert d["timestamp"] == "2026-06-20T10:00:00"

    def test_account_to_dict_no_timestamp(self):
        account = BrokerAccount(
            account_id="test_001",
            cash=Decimal("10000.00"),
            equity=Decimal("12000.00"),
            buying_power=Decimal("20000.00"),
        )
        d = account.to_dict()
        assert d["timestamp"] is None

    def test_account_is_frozen(self):
        account = BrokerAccount(
            account_id="test_001",
            cash=Decimal("10000.00"),
            equity=Decimal("12000.00"),
            buying_power=Decimal("20000.00"),
        )
        with pytest.raises(Exception):
            account.account_id = "new_id"


class TestBrokerPosition:
    def test_basic_position(self):
        pos = BrokerPosition(
            symbol="AAPL",
            quantity=Decimal("100"),
            avg_entry_price=Decimal("150.00"),
            current_price=Decimal("160.00"),
            market_value=Decimal("16000.00"),
            unrealized_pnl=Decimal("1000.00"),
        )
        assert pos.symbol == "AAPL"
        assert pos.quantity == Decimal("100")
        assert pos.market_value == Decimal("16000.00")
        assert pos.unrealized_pnl == Decimal("1000.00")

    def test_position_without_current_price(self):
        pos = BrokerPosition(
            symbol="AAPL",
            quantity=Decimal("100"),
            avg_entry_price=Decimal("150.00"),
        )
        assert pos.current_price is None
        assert pos.market_value is None
        assert pos.unrealized_pnl is None

    def test_position_to_dict(self):
        ts = datetime(2026, 6, 20, 10, 0, 0)
        pos = BrokerPosition(
            symbol="AAPL",
            quantity=Decimal("100"),
            avg_entry_price=Decimal("150.00"),
            current_price=Decimal("160.00"),
            market_value=Decimal("16000.00"),
            unrealized_pnl=Decimal("1000.00"),
            timestamp=ts,
        )
        d = pos.to_dict()
        assert d["symbol"] == "AAPL"
        assert d["quantity"] == 100.0
        assert d["avg_entry_price"] == 150.0
        assert d["current_price"] == 160.0
        assert d["market_value"] == 16000.0
        assert d["unrealized_pnl"] == 1000.0

    def test_position_is_frozen(self):
        pos = BrokerPosition(
            symbol="AAPL",
            quantity=Decimal("100"),
            avg_entry_price=Decimal("150.00"),
        )
        with pytest.raises(Exception):
            pos.symbol = "GOOGL"


class TestBrokerOrder:
    def test_basic_order(self):
        order = BrokerOrder(
            order_id="order_001",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("100"),
            filled_quantity=Decimal("100"),
            status=OrderStatus.FILLED,
        )
        assert order.order_id == "order_001"
        assert order.side == OrderSide.BUY
        assert order.status == OrderStatus.FILLED

    def test_order_with_price(self):
        order = BrokerOrder(
            order_id="order_001",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("100"),
            filled_quantity=Decimal("100"),
            status=OrderStatus.FILLED,
            price=Decimal("150.00"),
        )
        assert order.price == Decimal("150.00")

    def test_order_to_dict(self):
        ts = datetime(2026, 6, 20, 10, 0, 0)
        order = BrokerOrder(
            order_id="order_001",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("100"),
            filled_quantity=Decimal("100"),
            status=OrderStatus.FILLED,
            price=Decimal("150.00"),
            created_at=ts,
            updated_at=ts,
        )
        d = order.to_dict()
        assert d["order_id"] == "order_001"
        assert d["side"] == "BUY"
        assert d["order_type"] == "MARKET"
        assert d["status"] == "FILLED"
        assert d["price"] == 150.0

    def test_order_is_frozen(self):
        order = BrokerOrder(
            order_id="order_001",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("100"),
            filled_quantity=Decimal("100"),
            status=OrderStatus.FILLED,
        )
        with pytest.raises(Exception):
            order.order_id = "new_id"


class TestOrderPreview:
    def test_basic_preview(self):
        preview = OrderPreview(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("100"),
            order_type=OrderType.MARKET,
            estimated_price=Decimal("150.00"),
            estimated_total=Decimal("15000.00"),
            estimated_fees=Decimal("1.00"),
            buying_power_impact=Decimal("15001.00"),
            warnings=[],
        )
        assert preview.symbol == "AAPL"
        assert preview.warnings == []

    def test_preview_with_warnings(self):
        preview = OrderPreview(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("1000000"),
            order_type=OrderType.MARKET,
            estimated_price=Decimal("150.00"),
            estimated_total=Decimal("150000000.00"),
            estimated_fees=Decimal("1.00"),
            buying_power_impact=Decimal("150000001.00"),
            warnings=["Insufficient buying power"],
        )
        assert len(preview.warnings) == 1
        assert "Insufficient" in preview.warnings[0]

    def test_preview_to_dict(self):
        ts = datetime(2026, 6, 20, 10, 0, 0)
        preview = OrderPreview(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("100"),
            order_type=OrderType.MARKET,
            estimated_price=Decimal("150.00"),
            estimated_total=Decimal("15000.00"),
            estimated_fees=Decimal("1.00"),
            buying_power_impact=Decimal("15001.00"),
            warnings=["Test warning"],
            timestamp=ts,
        )
        d = preview.to_dict()
        assert d["symbol"] == "AAPL"
        assert d["side"] == "BUY"
        assert d["estimated_price"] == 150.0
        assert d["warnings"] == ["Test warning"]

    def test_preview_is_frozen(self):
        preview = OrderPreview(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("100"),
            order_type=OrderType.MARKET,
            estimated_price=Decimal("150.00"),
            estimated_total=Decimal("15000.00"),
            estimated_fees=Decimal("1.00"),
            buying_power_impact=Decimal("15001.00"),
            warnings=[],
        )
        with pytest.raises(Exception):
            preview.symbol = "GOOGL"


class TestBrokerAdapter:
    def test_is_abstract(self):
        assert hasattr(BrokerAdapter, "__abstractmethods__")
        assert len(BrokerAdapter.__abstractmethods__) > 0

    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            BrokerAdapter(mode=BrokerMode.PAPER, config={})

    def test_mode_set_in_init(self):
        class TestAdapter(BrokerAdapter):
            def is_authenticated(self): return True
            def get_account(self): return BrokerAccount("test", Decimal("0"), Decimal("0"), Decimal("0"))
            def get_positions(self): return []
            def get_orders(self, since=None): return []
            def get_order(self, order_id): return None
            def is_tradable(self, symbol): return True
            def get_quote(self, symbol): return {}
            def preview_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
                return OrderPreview(symbol, side, quantity, order_type, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), [])
            def submit_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
                return BrokerOrder("test", symbol, side, order_type, quantity, quantity, OrderStatus.FILLED)
            def cancel_order(self, order_id): return True
            def connect(self): return True
            def disconnect(self): pass

        adapter = TestAdapter(mode=BrokerMode.PAPER, config={"test": True})
        assert adapter.mode == BrokerMode.PAPER
        assert adapter.config == {"test": True}

    def test_is_live_false_by_default(self):
        class TestAdapter(BrokerAdapter):
            def is_authenticated(self): return True
            def get_account(self): return BrokerAccount("test", Decimal("0"), Decimal("0"), Decimal("0"))
            def get_positions(self): return []
            def get_orders(self, since=None): return []
            def get_order(self, order_id): return None
            def is_tradable(self, symbol): return True
            def get_quote(self, symbol): return {}
            def preview_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
                return OrderPreview(symbol, side, quantity, order_type, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), [])
            def submit_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
                return BrokerOrder("test", symbol, side, order_type, quantity, quantity, OrderStatus.FILLED)
            def cancel_order(self, order_id): return True
            def connect(self): return True
            def disconnect(self): pass

        adapter = TestAdapter(mode=BrokerMode.PAPER, config={})
        assert adapter.is_live is False

    def test_is_live_true_for_live_mode(self):
        class TestAdapter(BrokerAdapter):
            def is_authenticated(self): return True
            def get_account(self): return BrokerAccount("test", Decimal("0"), Decimal("0"), Decimal("0"))
            def get_positions(self): return []
            def get_orders(self, since=None): return []
            def get_order(self, order_id): return None
            def is_tradable(self, symbol): return True
            def get_quote(self, symbol): return {}
            def preview_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
                return OrderPreview(symbol, side, quantity, order_type, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), [])
            def submit_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
                return BrokerOrder("test", symbol, side, order_type, quantity, quantity, OrderStatus.FILLED)
            def cancel_order(self, order_id): return True
            def connect(self): return True
            def disconnect(self): pass

        adapter = TestAdapter(mode=BrokerMode.LIVE, config={})
        adapter.enable_live()
        assert adapter.is_live is True

    def test_enable_live_raises_for_non_live_mode(self):
        class TestAdapter(BrokerAdapter):
            def is_authenticated(self): return True
            def get_account(self): return BrokerAccount("test", Decimal("0"), Decimal("0"), Decimal("0"))
            def get_positions(self): return []
            def get_orders(self, since=None): return []
            def get_order(self, order_id): return None
            def is_tradable(self, symbol): return True
            def get_quote(self, symbol): return {}
            def preview_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
                return OrderPreview(symbol, side, quantity, order_type, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), [])
            def submit_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
                return BrokerOrder("test", symbol, side, order_type, quantity, quantity, OrderStatus.FILLED)
            def cancel_order(self, order_id): return True
            def connect(self): return True
            def disconnect(self): pass

        adapter = TestAdapter(mode=BrokerMode.PAPER, config={})
        with pytest.raises(RuntimeError):
            adapter.enable_live()

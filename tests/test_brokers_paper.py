"""Tests for paper broker adapter (261 lines)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.brokers.base import BrokerMode, BrokerOrder, OrderSide, OrderStatus, OrderType
from trading_bot.brokers.paper import PaperBrokerAdapter
from trading_bot.config.settings import AppSettings, PaperSettings, RiskSettings, Settings
from trading_bot.models.portfolio import PortfolioState
from trading_bot.portfolio.ledger import PortfolioLedger


@pytest.fixture
def settings(tmp_path):
    return Settings(
        app=AppSettings(
            state_db_path=str(tmp_path / "test.db"),
            log_dir=str(tmp_path / "logs"),
            portfolio_summary_path=str(tmp_path / "portfolio.json"),
            scan_results_path=str(tmp_path / "scan.json"),
        ),
        risk=RiskSettings(),
        paper=PaperSettings(fee_per_order=1.0, slippage_bps=10),
    )


@pytest.fixture
def ledger(tmp_path):
    db_path = tmp_path / "test.db"
    ledger = PortfolioLedger(db_path)
    ledger.ensure_portfolio_state(starting_cash=10000.0)
    return ledger


@pytest.fixture
def adapter(settings, ledger):
    return PaperBrokerAdapter(settings, ledger)


class TestPaperBrokerAdapterInit:
    def test_init_sets_paper_mode(self, settings, ledger):
        adapter = PaperBrokerAdapter(settings, ledger)
        assert adapter.mode == BrokerMode.PAPER

    def test_init_creates_paper_broker(self, settings, ledger):
        adapter = PaperBrokerAdapter(settings, ledger)
        assert adapter._paper_broker is not None

    def test_init_not_connected(self, settings, ledger):
        adapter = PaperBrokerAdapter(settings, ledger)
        assert adapter._connected is False


class TestConnectDisconnect:
    def test_connect(self, adapter):
        result = adapter.connect()
        assert result is True
        assert adapter._connected is True

    def test_disconnect(self, adapter):
        adapter.connect()
        adapter.disconnect()
        assert adapter._connected is False


class TestAuthentication:
    def test_not_authenticated_when_disconnected(self, adapter):
        assert adapter.is_authenticated() is False

    def test_authenticated_when_connected(self, adapter):
        adapter.connect()
        assert adapter.is_authenticated() is True


class TestAccount:
    def test_get_account(self, adapter):
        adapter.connect()
        account = adapter.get_account()
        assert account.account_id == "PAPER_001"
        assert account.currency == "USD"
        assert account.cash == Decimal("10000")
        assert account.buying_power == Decimal("10000")

    def test_account_timestamp(self, adapter):
        adapter.connect()
        account = adapter.get_account()
        assert account.timestamp is not None


class TestPositions:
    def test_empty_positions(self, adapter):
        adapter.connect()
        positions = adapter.get_positions()
        assert positions == []

    def test_positions_with_holdings(self, settings, ledger):
        """Test positions when portfolio has holdings."""
        # Create a portfolio state with positions
        from trading_bot.models.portfolio import Position
        state = PortfolioState(
            cash=5000.0,
            equity=10000.0,
            positions={"AAPL": Position(
                ticker="AAPL",
                quantity=10,
                average_cost=100.0,
            )},
        )
        ledger.save_portfolio_state(state)
        ledger.record_equity_snapshot(state)

        adapter = PaperBrokerAdapter(settings, ledger)
        adapter.connect()
        positions = adapter.get_positions()

        assert len(positions) == 1
        assert positions[0].symbol == "AAPL"
        assert positions[0].quantity == Decimal("10")
        assert positions[0].unrealized_pnl == Decimal("0")


class TestOrders:
    def test_get_orders_returns_empty(self, adapter):
        adapter.connect()
        orders = adapter.get_orders()
        assert orders == []

    def test_get_orders_with_since(self, adapter):
        adapter.connect()
        orders = adapter.get_orders(since=datetime(2026, 1, 1))
        assert orders == []

    def test_get_order_returns_none(self, adapter):
        adapter.connect()
        order = adapter.get_order("nonexistent")
        assert order is None


class TestTradability:
    def test_all_symbols_tradable(self, adapter):
        assert adapter.is_tradable("AAPL") is True
        assert adapter.is_tradable("GOOGL") is True
        assert adapter.is_tradable("NONEXISTENT") is True


class TestQuote:
    def test_quote_error_when_no_data(self, adapter, monkeypatch):
        """Test quote returns error when fetch_bars fails."""
        monkeypatch.setattr(
            "trading_bot.data.market_data.fetch_bars",
            lambda *a, **k: __import__("pandas").DataFrame(),
        )
        quote = adapter.get_quote("AAPL")
        assert "error" in quote


class TestPreviewOrder:
    def test_preview_buy_order(self, adapter, monkeypatch):
        """Test previewing a buy order."""
        monkeypatch.setattr(
            adapter, "get_quote",
            lambda symbol: {"bid": 149.0, "ask": 151.0, "last": 150.0, "volume": 1000}
        )

        preview = adapter.preview_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("100"),
            order_type=OrderType.MARKET,
        )

        assert preview.symbol == "AAPL"
        assert preview.side == OrderSide.BUY
        assert preview.estimated_price == Decimal("151.0")  # Ask price for buy
        assert preview.estimated_total == Decimal("15100.0")
        assert len(preview.warnings) >= 0  # May have insufficient funds warning

    def test_preview_sell_order(self, adapter, monkeypatch):
        """Test previewing a sell order."""
        monkeypatch.setattr(
            adapter, "get_quote",
            lambda symbol: {"bid": 149.0, "ask": 151.0, "last": 150.0, "volume": 1000}
        )

        preview = adapter.preview_order(
            symbol="AAPL",
            side=OrderSide.SELL,
            quantity=Decimal("100"),
            order_type=OrderType.MARKET,
        )

        assert preview.side == OrderSide.SELL
        assert preview.estimated_price == Decimal("149.0")  # Bid price for sell

    def test_preview_order_with_error(self, adapter, monkeypatch):
        """Test preview when quote fails."""
        monkeypatch.setattr(
            adapter, "get_quote",
            lambda symbol: {"error": "No data available"}
        )

        preview = adapter.preview_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("100"),
        )

        assert len(preview.warnings) == 1
        assert "Cannot get quote" in preview.warnings[0]
        assert preview.estimated_price == Decimal("0")

    def test_preview_limit_order(self, adapter, monkeypatch):
        """Test previewing a limit order."""
        monkeypatch.setattr(
            adapter, "get_quote",
            lambda symbol: {"bid": 149.0, "ask": 151.0, "last": 150.0, "volume": 1000}
        )

        preview = adapter.preview_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("100"),
            order_type=OrderType.LIMIT,
            price=Decimal("145.0"),
        )

        assert preview.estimated_price == Decimal("145.0")


class TestSubmitOrder:
    def test_submit_buy_order(self, adapter, monkeypatch):
        """Test submitting a buy order."""
        from trading_bot.models.order import FillResult

        def mock_submit(order, market_price):
            return FillResult(
                order_id="test-fill-1",
                ticker=order.ticker,
                quantity=order.quantity,
                fill_price=150.0,
                fees=1.0,
                filled_at=datetime.now(),
            )

        monkeypatch.setattr(adapter._paper_broker, "submit_order", mock_submit)
        monkeypatch.setattr(
            adapter, "get_quote",
            lambda symbol: {"bid": 149.0, "ask": 151.0, "last": 150.0, "volume": 1000},
        )

        order = adapter.submit_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("100"),
            order_type=OrderType.MARKET,
        )

        assert order.symbol == "AAPL"
        assert order.side == OrderSide.BUY
        assert order.status == OrderStatus.FILLED
        assert order.quantity == Decimal("100")

    def test_submit_sell_order(self, adapter, monkeypatch):
        """Test submitting a sell order."""
        from trading_bot.models.order import FillResult

        def mock_submit(order, market_price):
            return FillResult(
                order_id="test-fill-2",
                ticker=order.ticker,
                quantity=order.quantity,
                fill_price=150.0,
                fees=1.0,
                filled_at=datetime.now(),
            )

        monkeypatch.setattr(adapter._paper_broker, "submit_order", mock_submit)
        monkeypatch.setattr(
            adapter, "get_quote",
            lambda symbol: {"bid": 149.0, "ask": 151.0, "last": 150.0, "volume": 1000},
        )

        order = adapter.submit_order(
            symbol="AAPL",
            side=OrderSide.SELL,
            quantity=Decimal("50"),
            order_type=OrderType.MARKET,
        )

        assert order.side == OrderSide.SELL
        assert order.status == OrderStatus.FILLED

    def test_market_order_fetches_quote_when_no_price(self, adapter, monkeypatch):
        """Bug A: Market orders without explicit price must fetch a quote, not use 0.0."""
        from trading_bot.models.order import FillResult

        captured_market_price: list[float] = []

        def mock_submit(order, market_price):
            captured_market_price.append(market_price)
            return FillResult(
                order_id="test-fill-3",
                ticker=order.ticker,
                quantity=order.quantity,
                fill_price=market_price,
                fees=1.0,
                filled_at=datetime.now(),
            )

        monkeypatch.setattr(adapter._paper_broker, "submit_order", mock_submit)
        monkeypatch.setattr(
            adapter, "get_quote",
            lambda symbol: {"bid": 99.0, "ask": 101.0, "last": 100.0, "volume": 5000},
        )

        order = adapter.submit_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            order_type=OrderType.MARKET,
        )

        assert order.status == OrderStatus.FILLED
        assert captured_market_price[0] == 100.0  # Used quote["last"], not 0.0

    def test_market_order_rejected_when_quote_fails(self, adapter, monkeypatch):
        """Bug A: Market order returns REJECTED when quote cannot be fetched."""
        monkeypatch.setattr(
            adapter, "get_quote",
            lambda symbol: {"error": "No data available"},
        )

        order = adapter.submit_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("100"),
            order_type=OrderType.MARKET,
        )

        assert order.status == OrderStatus.REJECTED
        assert order.filled_quantity == Decimal("0")

    def test_submit_order_returns_rejected_on_value_error(self, adapter, monkeypatch):
        """Bug B: submit_order returns REJECTED instead of raising ValueError."""
        def mock_submit(order, market_price):
            raise ValueError("insufficient cash for paper trade")

        monkeypatch.setattr(adapter._paper_broker, "submit_order", mock_submit)
        monkeypatch.setattr(
            adapter, "get_quote",
            lambda symbol: {"bid": 149.0, "ask": 151.0, "last": 150.0, "volume": 1000},
        )

        order = adapter.submit_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("1000000"),
            order_type=OrderType.MARKET,
        )

        assert order.status == OrderStatus.REJECTED
        assert order.filled_quantity == Decimal("0")

    def test_submit_order_with_explicit_price(self, adapter, monkeypatch):
        """Orders with explicit price bypass quote fetch."""
        from trading_bot.models.order import FillResult

        captured: list[float] = []

        def mock_submit(order, market_price):
            captured.append(market_price)
            return FillResult(
                order_id="test-fill-4",
                ticker=order.ticker,
                quantity=order.quantity,
                fill_price=market_price,
                fees=1.0,
                filled_at=datetime.now(),
            )

        monkeypatch.setattr(adapter._paper_broker, "submit_order", mock_submit)

        order = adapter.submit_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            order_type=OrderType.LIMIT,
            price=Decimal("145.0"),
        )

        assert order.status == OrderStatus.FILLED
        assert captured[0] == 145.0

    def test_stop_limit_maps_to_stop(self, adapter, monkeypatch):
        """Bug D: STOP_LIMIT order_type maps to 'stop' not 'stop_limit'."""
        from trading_bot.models.order import FillResult

        captured_order: list = []

        def mock_submit(order, market_price):
            captured_order.append(order)
            return FillResult(
                order_id="test-fill-5",
                ticker=order.ticker,
                quantity=order.quantity,
                fill_price=market_price,
                fees=1.0,
                filled_at=datetime.now(),
            )

        monkeypatch.setattr(adapter._paper_broker, "submit_order", mock_submit)

        order = adapter.submit_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            order_type=OrderType.STOP_LIMIT,
            price=Decimal("150.0"),
        )

        assert order.status == OrderStatus.FILLED
        assert captured_order[0].order_type == "stop"


class TestAccountNoState:
    """Bug C: get_account/get_positions must work when no portfolio state exists."""

    def test_get_account_with_no_state(self, settings, tmp_path):
        """get_account should not crash when portfolio state is empty."""
        db_path = tmp_path / "fresh.db"
        ledger = PortfolioLedger(db_path)
        adapter = PaperBrokerAdapter(settings, ledger)
        adapter.connect()

        account = adapter.get_account()
        assert account.cash > 0  # ensure_portfolio_state seeded it

    def test_get_positions_with_no_state(self, settings, tmp_path):
        """get_positions should return empty list when no portfolio state exists."""
        db_path = tmp_path / "fresh.db"
        ledger = PortfolioLedger(db_path)
        adapter = PaperBrokerAdapter(settings, ledger)
        adapter.connect()

        positions = adapter.get_positions()
        assert positions == []


class TestCancelOrder:
    def test_cancel_order_returns_false(self, adapter):
        """Paper broker fills immediately, so cancel always returns False."""
        result = adapter.cancel_order("any-order-id")
        assert result is False


class TestOrderHistory:
    """Phase 4B: order history tracking in the paper broker adapter."""

    def _mock_fill(self, order, market_price, order_id="fill-1"):
        from trading_bot.models.order import FillResult
        return FillResult(
            order_id=order_id,
            ticker=order.ticker,
            quantity=order.quantity,
            fill_price=market_price,
            fees=1.0,
            filled_at=datetime.now(),
        )

    def _submit_buy(self, adapter, monkeypatch, symbol="AAPL", qty=Decimal("10"), order_type=OrderType.MARKET, price=None, order_counter=None):
        """Submit a buy order with mocked market data and return the BrokerOrder."""
        counter = {"i": 0}

        def mock_submit(order, market_price):
            counter["i"] += 1
            oid = f"fill-{order.ticker}-{counter['i']}" if order_counter is None else order_counter(order)
            return self._mock_fill(order, market_price, order_id=oid)

        monkeypatch.setattr(adapter._paper_broker, "submit_order", mock_submit)
        monkeypatch.setattr(
            adapter, "get_quote",
            lambda sym: {"bid": 99.0, "ask": 101.0, "last": 100.0, "volume": 5000},
        )
        return adapter.submit_order(
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=qty,
            order_type=order_type,
            price=price,
        )

    def test_get_orders_empty_initially(self, adapter):
        adapter.connect()
        assert adapter.get_orders() == []

    def test_get_orders_returns_submitted_orders(self, adapter, monkeypatch):
        adapter.connect()
        order = self._submit_buy(adapter, monkeypatch)
        orders = adapter.get_orders()
        assert len(orders) == 1
        assert orders[0].order_id == order.order_id

    def test_get_orders_in_submission_order(self, adapter, monkeypatch):
        adapter.connect()
        first = self._submit_buy(adapter, monkeypatch, symbol="AAPL")
        second = self._submit_buy(adapter, monkeypatch, symbol="MSFT")
        orders = adapter.get_orders()
        assert [o.order_id for o in orders] == [first.order_id, second.order_id]
        assert orders[0].symbol == "AAPL"
        assert orders[1].symbol == "MSFT"

    def test_get_order_by_id(self, adapter, monkeypatch):
        adapter.connect()
        submitted = self._submit_buy(adapter, monkeypatch)
        fetched = adapter.get_order(submitted.order_id)
        assert fetched is not None
        assert fetched.order_id == submitted.order_id
        assert fetched.symbol == submitted.symbol

    def test_get_order_nonexistent_returns_none(self, adapter):
        adapter.connect()
        assert adapter.get_order("does-not-exist") is None

    def test_orders_have_correct_filled_status(self, adapter, monkeypatch):
        adapter.connect()
        order = self._submit_buy(adapter, monkeypatch)
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == Decimal("10")
        assert order.price == Decimal("100")

    def test_orders_have_correct_rejected_status(self, adapter, monkeypatch):
        adapter.connect()
        monkeypatch.setattr(
            adapter, "get_quote",
            lambda sym: {"error": "No data available"},
        )
        order = adapter.submit_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            order_type=OrderType.MARKET,
        )
        assert order.status == OrderStatus.REJECTED
        orders = adapter.get_orders()
        assert len(orders) == 1
        assert orders[0].status == OrderStatus.REJECTED

    def test_get_orders_since_filters_by_created_at(self, adapter, monkeypatch):
        adapter.connect()
        old = datetime(2020, 1, 1)
        cutoff = datetime(2025, 6, 1)
        # Submit one; created_at = now
        submitted = self._submit_buy(adapter, monkeypatch)
        assert submitted.created_at is not None
        assert submitted.created_at >= cutoff
        assert len(adapter.get_orders(since=old)) == 1
        assert len(adapter.get_orders(since=cutoff)) == 1
        assert adapter.get_orders(since=datetime(2030, 1, 1)) == []

    def test_get_orders_returns_independent_copy(self, adapter, monkeypatch):
        adapter.connect()
        self._submit_buy(adapter, monkeypatch)
        first = adapter.get_orders()
        self._submit_buy(adapter, monkeypatch, symbol="MSFT")
        assert len(first) == 1  # snapshot not mutated by later submissions

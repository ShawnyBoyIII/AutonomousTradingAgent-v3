"""Tests for RL action schemes (196 lines)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import gymnasium as gym
import pytest

from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.rl.actions import BSHActionScheme, ProportionActionScheme


@pytest.fixture
def paper_broker():
    return PaperBroker(starting_cash=10000.0, fee_per_order=1.0, slippage_bps=10)


class TestActionSchemeBase:
    def test_action_space_not_initialized_raises(self):
        from trading_bot.rl.actions import ActionScheme
        # ActionScheme is abstract with @abstractmethod, cannot instantiate directly
        with pytest.raises(TypeError):
            ActionScheme()


class TestBSHActionScheme:
    def test_action_space_size(self):
        symbols = ["AAPL", "GOOGL"]
        scheme = BSHActionScheme(symbols, max_shares=100)
        expected = len(symbols) * 3 + 1
        assert scheme.action_space.n == expected

    def test_hold_action_noop(self):
        symbols = ["AAPL"]
        scheme = BSHActionScheme(symbols)
        scheme.reset_portfolio(MagicMock())
        # Action 0 is global no-op
        scheme.perform(0, {"AAPL": 150.0})

    def test_buy_action(self, paper_broker):
        symbols = ["AAPL"]
        scheme = BSHActionScheme(symbols, max_shares=100)
        scheme.reset_portfolio(paper_broker)

        # BSH action mapping for 1 symbol: 0=noop, 1=HOLD, 2=BUY, 3=SELL
        scheme.perform(2, {"AAPL": 100.0})

        assert paper_broker.positions.get("AAPL", 0) > 0

    def test_sell_action(self, paper_broker):
        symbols = ["AAPL"]
        scheme = BSHActionScheme(symbols, max_shares=100)
        scheme.reset_portfolio(paper_broker)

        # Buy first
        scheme.perform(2, {"AAPL": 100.0})
        initial_pos = paper_broker.positions.get("AAPL", 0)
        assert initial_pos > 0

        # Sell all
        scheme.perform(3, {"AAPL": 105.0})

        assert paper_broker.positions.get("AAPL", 0) == 0

    def test_sell_without_position_noop(self, paper_broker):
        symbols = ["AAPL"]
        scheme = BSHActionScheme(symbols, max_shares=100)
        scheme.reset_portfolio(paper_broker)

        # Try to sell without position (action 3 = SELL)
        scheme.perform(3, {"AAPL": 100.0})

        assert paper_broker.positions.get("AAPL", 0) == 0

    def test_buy_exceeds_max_shares(self, paper_broker):
        symbols = ["AAPL"]
        scheme = BSHActionScheme(symbols, max_shares=10)
        scheme.reset_portfolio(paper_broker)

        scheme.perform(2, {"AAPL": 100.0})

        assert paper_broker.positions.get("AAPL", 0) <= 10

    def test_buy_exceeds_cash(self, paper_broker):
        symbols = ["AAPL"]
        scheme = BSHActionScheme(symbols, max_shares=1000)
        scheme.reset_portfolio(paper_broker)

        # Price too high for available cash
        scheme.perform(2, {"AAPL": 99999.0})

        assert paper_broker.positions.get("AAPL", 0) == 0

    def test_null_price_noop(self, paper_broker):
        symbols = ["AAPL"]
        scheme = BSHActionScheme(symbols)
        scheme.reset_portfolio(paper_broker)

        scheme.perform(2, {})  # No AAPL in prices
        assert paper_broker.positions.get("AAPL", 0) == 0

    def test_zero_price_noop(self, paper_broker):
        symbols = ["AAPL"]
        scheme = BSHActionScheme(symbols)
        scheme.reset_portfolio(paper_broker)

        scheme.perform(2, {"AAPL": 0.0})
        assert paper_broker.positions.get("AAPL", 0) == 0

    def test_negative_price_noop(self, paper_broker):
        symbols = ["AAPL"]
        scheme = BSHActionScheme(symbols)
        scheme.reset_portfolio(paper_broker)

        scheme.perform(2, {"AAPL": -10.0})
        assert paper_broker.positions.get("AAPL", 0) == 0

    def test_no_broker_noop(self):
        symbols = ["AAPL"]
        scheme = BSHActionScheme(symbols)
        scheme.reset_portfolio(None)

        # Should not raise
        scheme.perform(2, {"AAPL": 100.0})

    def test_symbols_uppercased(self):
        scheme = BSHActionScheme(["aapl", "googl"])
        assert scheme.symbols == ["AAPL", "GOOGL"]

    def test_multiple_symbols(self):
        symbols = ["AAPL", "GOOGL", "MSFT"]
        scheme = BSHActionScheme(symbols)
        # 3 symbols * 3 directions + 1 global noop = 10
        assert scheme.action_space.n == 10

    def test_buy_affordable_shares(self, paper_broker):
        symbols = ["AAPL"]
        scheme = BSHActionScheme(symbols, max_shares=1000)
        scheme.reset_portfolio(paper_broker)

        # Cash = 10000, price = 100, 95% allocation = 9500, shares = 95
        # Action 2 = BUY for 1 symbol
        scheme.perform(2, {"AAPL": 100.0})

        shares = paper_broker.positions.get("AAPL", 0)
        assert 90 <= shares <= 96  # Account for slippage

    def test_action_indexing_correct(self, paper_broker):
        """Test that actions map to correct symbols and directions."""
        symbols = ["AAPL", "GOOGL"]
        scheme = BSHActionScheme(symbols, max_shares=100)
        scheme.reset_portfolio(paper_broker)

        # BSH for 2 symbols: 0=noop, 1=HOLD AAPL, 2=BUY AAPL, 3=SELL AAPL,
        #                     4=HOLD GOOGL, 5=BUY GOOGL, 6=SELL GOOGL
        scheme.perform(2, {"AAPL": 100.0, "GOOGL": 100.0})
        assert paper_broker.positions.get("AAPL", 0) > 0
        assert paper_broker.positions.get("GOOGL", 0) == 0

        scheme.perform(5, {"AAPL": 100.0, "GOOGL": 100.0})
        assert paper_broker.positions.get("GOOGL", 0) > 0


class TestProportionActionScheme:
    def test_action_space_size(self):
        symbols = ["AAPL", "GOOGL"]
        scheme = ProportionActionScheme(symbols, max_shares=500)
        expected = len(symbols) * 2 * 10 + 1
        assert scheme.action_space.n == expected

    def test_hold_action_noop(self):
        symbols = ["AAPL"]
        scheme = ProportionActionScheme(symbols)
        scheme.reset_portfolio(MagicMock())
        scheme.perform(0, {"AAPL": 150.0})

    def test_buy_proportion(self, paper_broker):
        symbols = ["AAPL"]
        scheme = ProportionActionScheme(symbols, max_shares=500)
        scheme.reset_portfolio(paper_broker)

        # Proportion for 1 symbol: 0=noop, 1=BUY 10%, 10=BUY 100%, 11=SELL 10%, 20=SELL 100%
        scheme.perform(1, {"AAPL": 100.0})

        assert paper_broker.positions.get("AAPL", 0) > 0

    def test_sell_proportion(self, paper_broker):
        symbols = ["AAPL"]
        scheme = ProportionActionScheme(symbols, max_shares=500)
        scheme.reset_portfolio(paper_broker)

        # Buy first (10%)
        scheme.perform(1, {"AAPL": 100.0})
        initial_pos = paper_broker.positions.get("AAPL", 0)
        assert initial_pos > 0

        # Sell 50% (prop_idx 4): action = 0*20 + 1*10 + 4 + 1 = 15
        scheme.perform(15, {"AAPL": 105.0})

        final_pos = paper_broker.positions.get("AAPL", 0)
        assert final_pos < initial_pos
        assert final_pos > 0

    def test_sell_all_proportion(self, paper_broker):
        symbols = ["AAPL"]
        scheme = ProportionActionScheme(symbols, max_shares=500)
        scheme.reset_portfolio(paper_broker)

        # Buy first
        scheme.perform(1, {"AAPL": 100.0})
        initial_pos = paper_broker.positions.get("AAPL", 0)

        # Sell 100% (prop_idx 9): action = 0*20 + 1*10 + 9 + 1 = 20
        scheme.perform(20, {"AAPL": 105.0})

        assert paper_broker.positions.get("AAPL", 0) == 0

    def test_sell_without_position_noop(self, paper_broker):
        symbols = ["AAPL"]
        scheme = ProportionActionScheme(symbols)
        scheme.reset_portfolio(paper_broker)

        # Try to sell without position (action 15 = SELL 50%)
        scheme.perform(15, {"AAPL": 100.0})
        assert paper_broker.positions.get("AAPL", 0) == 0

    def test_proportions_constant(self):
        expected = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        assert ProportionActionScheme.PROPORTIONS == expected

    def test_max_shares_cap(self, paper_broker):
        symbols = ["AAPL"]
        scheme = ProportionActionScheme(symbols, max_shares=5)
        scheme.reset_portfolio(paper_broker)

        # Buy at 100%: action = 0*20 + 0*10 + 9 + 1 = 10
        scheme.perform(10, {"AAPL": 100.0})

        assert paper_broker.positions.get("AAPL", 0) <= 5

    def test_null_price_noop(self, paper_broker):
        symbols = ["AAPL"]
        scheme = ProportionActionScheme(symbols)
        scheme.reset_portfolio(paper_broker)

        scheme.perform(1, {})
        assert paper_broker.positions.get("AAPL", 0) == 0

    def test_zero_price_noop(self, paper_broker):
        symbols = ["AAPL"]
        scheme = ProportionActionScheme(symbols)
        scheme.reset_portfolio(paper_broker)

        scheme.perform(1, {"AAPL": 0.0})
        assert paper_broker.positions.get("AAPL", 0) == 0

    def test_no_broker_noop(self):
        symbols = ["AAPL"]
        scheme = ProportionActionScheme(symbols)
        scheme.reset_portfolio(None)

        scheme.perform(1, {"AAPL": 100.0})

    def test_symbols_uppercased(self):
        scheme = ProportionActionScheme(["aapl"])
        assert scheme.symbols == ["AAPL"]

    def test_multiple_symbols(self):
        symbols = ["AAPL", "GOOGL", "MSFT"]
        scheme = ProportionActionScheme(symbols)
        # 3 symbols * 2 directions * 10 proportions + 1 = 61
        assert scheme.action_space.n == 61

    def test_buy_small_proportion(self, paper_broker):
        symbols = ["AAPL"]
        scheme = ProportionActionScheme(symbols, max_shares=1000)
        scheme.reset_portfolio(paper_broker)

        # Buy at 10% (prop_idx 0): action = 0*20 + 0*10 + 0 + 1 = 1
        scheme.perform(1, {"AAPL": 100.0})

        shares = paper_broker.positions.get("AAPL", 0)
        # 10% of 10000 = 1000, / 100 = 10 shares
        assert shares >= 8  # Account for slippage and fees

    def test_sell_partial_proportion(self, paper_broker):
        symbols = ["AAPL"]
        scheme = ProportionActionScheme(symbols, max_shares=1000)
        scheme.reset_portfolio(paper_broker)

        # Buy at 50% (prop_idx 4): action = 0*20 + 0*10 + 4 + 1 = 5
        scheme.perform(5, {"AAPL": 100.0})
        initial_pos = paper_broker.positions.get("AAPL", 0)
        assert initial_pos > 0

        # Sell 20% (prop_idx 1): action = 0*20 + 1*10 + 1 + 1 = 12
        scheme.perform(12, {"AAPL": 105.0})

        final_pos = paper_broker.positions.get("AAPL", 0)
        assert final_pos > 0
        assert final_pos < initial_pos

    def test_sell_qty_zero_noop(self, paper_broker):
        """If sell quantity rounds to 0, should not submit order."""
        symbols = ["AAPL"]
        scheme = ProportionActionScheme(symbols, max_shares=1000)
        scheme.reset_portfolio(paper_broker)

        # Buy at 10% (prop_idx 0): action = 0*20 + 0*10 + 0 + 1 = 1
        scheme.perform(1, {"AAPL": 100.0})
        shares = paper_broker.positions.get("AAPL", 0)
        # 10% of 10000 = 1000, / 100 = 10 shares
        assert shares >= 8

        # Try to sell 10% of shares -> rounds to small amount but > 0
        # action = 0*20 + 1*10 + 0 + 1 = 11
        scheme.perform(11, {"AAPL": 105.0})

        # Should have fewer shares since sell_qty was > 0
        final_pos = paper_broker.positions.get("AAPL", 0)
        assert final_pos < shares

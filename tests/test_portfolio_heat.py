"""Tests for portfolio heat fail-closed behaviour (Phase 2)."""

from __future__ import annotations

from unittest.mock import patch

from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.portfolio.performance import (
    compute_exposure_ratio,
    compute_portfolio_heat,
    compute_position_market_value,
    compute_unrealized_pnl,
)
from trading_bot.runtime.orchestrator import _calculate_portfolio_heat


class TestPortfolioPerformanceMath:
    """Portfolio math helpers cover long/short and invalid-equity cases."""

    def test_unrealized_pnl_for_long_and_short_positions(self) -> None:
        assert compute_unrealized_pnl(quantity=10, average_cost=100.0, market_price=150.0) == 500.0
        assert compute_unrealized_pnl(quantity=10, average_cost=100.0, market_price=80.0) == -200.0
        assert compute_unrealized_pnl(quantity=10, average_cost=100.0, market_price=100.0) == 0.0
        assert compute_unrealized_pnl(quantity=-10, average_cost=100.0, market_price=80.0) == 200.0
        assert compute_unrealized_pnl(quantity=-10, average_cost=100.0, market_price=150.0) == -500.0
        assert compute_unrealized_pnl(quantity=0, average_cost=100.0, market_price=150.0) == 0.0

    def test_position_market_value_keeps_position_direction(self) -> None:
        assert compute_position_market_value(quantity=10, market_price=150.0) == 1500.0
        assert compute_position_market_value(quantity=-10, market_price=150.0) == -1500.0
        assert compute_position_market_value(quantity=0, market_price=150.0) == 0.0
        assert compute_position_market_value(quantity=10, market_price=0.0) == 0.0

    def test_exposure_ratio_fails_closed_when_equity_is_not_positive(self) -> None:
        assert compute_exposure_ratio(market_value=50_000.0, equity=100_000.0) == 0.5
        assert compute_exposure_ratio(market_value=100_000.0, equity=100_000.0) == 1.0
        assert compute_exposure_ratio(market_value=150_000.0, equity=100_000.0) == 1.5
        assert compute_exposure_ratio(market_value=0.0, equity=100_000.0) == 0.0
        assert compute_exposure_ratio(market_value=-50_000.0, equity=100_000.0) == -0.5
        assert compute_exposure_ratio(market_value=50_000.0, equity=0.0) == 0.0
        assert compute_exposure_ratio(market_value=50_000.0, equity=-10_000.0) == 0.0


class TestComputePortfolioHeat:
    """compute_portfolio_heat respects heat_multiplier."""

    def test_normal_heat(self) -> None:
        positions = {"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0)}
        prices = {"AAPL": 140.0}  # -$100 loss
        heat = compute_portfolio_heat(positions, prices, 100_000.0)
        assert heat == 100.0 / 100_000.0  # 0.1%

    def test_heat_multiplier_doubles_loss(self) -> None:
        positions = {"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0)}
        prices = {"AAPL": 140.0}
        heat = compute_portfolio_heat(positions, prices, 100_000.0, heat_multiplier=2.0)
        assert heat == 200.0 / 100_000.0  # 0.2%

    def test_heat_multiplier_zero(self) -> None:
        positions = {"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0)}
        prices = {"AAPL": 140.0}
        heat = compute_portfolio_heat(positions, prices, 100_000.0, heat_multiplier=0.0)
        assert heat == 0.0

    def test_profitable_position_no_heat(self) -> None:
        positions = {"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0)}
        prices = {"AAPL": 160.0}  # +$100 gain
        heat = compute_portfolio_heat(positions, prices, 100_000.0)
        assert heat == 0.0  # gains don't count

    def test_zero_equity_returns_zero(self) -> None:
        positions = {"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0)}
        prices = {"AAPL": 140.0}
        heat = compute_portfolio_heat(positions, prices, 0.0)
        assert heat == 0.0

    def test_missing_price_falls_back_to_avg_cost(self) -> None:
        """When price is missing, falls back to average_cost (0 heat)."""
        positions = {"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0)}
        prices: dict[str, float] = {}  # no price for AAPL
        heat = compute_portfolio_heat(positions, prices, 100_000.0)
        assert heat == 0.0  # fallback = average_cost = 0 unrealized P&L


class TestCalculatePortfolioHeatFailClosed:
    """_calculate_portfolio_heat uses stop-loss on fetch failure."""

    def _make_state(self, positions: dict[str, Position]) -> PortfolioState:
        total_value = sum(
            p.quantity * p.average_cost for p in positions.values()
        )
        return PortfolioState(
            cash=100_000.0 - total_value,
            equity=100_000.0,
            positions=positions,
        )

    def _make_settings(self, fetch_raises: bool = False) -> None:
        """Patch market_data.fetch_and_validate_bars to simulate failure."""
        from trading_bot.data import market_data
        from trading_bot.data.validation import ValidationResult

        if fetch_raises:
            self._orig_fetch = market_data.fetch_and_validate_bars
            market_data.fetch_and_validate_bars = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network"))
        else:
            self._orig_fetch = market_data.fetch_and_validate_bars
            market_data.fetch_and_validate_bars = lambda *a, **k: (
                type("Frame", (), {"empty": True, "columns": []})(),
                DataValidationResult(valid=False, reason="faked"),
            )

    def _restore_fetch(self) -> None:
        from trading_bot.data import market_data
        market_data.fetch_and_validate_bars = self._orig_fetch  # type: ignore[attr-defined]

    def test_fresh_price_used_when_available(self) -> None:
        """When price fetch succeeds, uses the fresh price."""
        from trading_bot.data import market_data
        from trading_bot.data.validation import ValidationResult
        import pandas as pd

        positions = {"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0, stop_loss=140.0)}
        state = self._make_state(positions)

        from trading_bot.config.settings import Settings, AppSettings, MarketDataSettings, RiskSettings

        settings = Settings(
            app=AppSettings(state_db_path="state/test.db", log_dir="logs"),
            market_data=MarketDataSettings(intraday_period="5d", intraday_interval="5m"),
            risk=RiskSettings(max_portfolio_heat_pct=0.03),
        )

        def fake_fetch(*a, **k):
            frame = pd.DataFrame({"close": [145.0], "volume": [1000]})
            return frame, ValidationResult(valid=True, reason="ok")

        with patch.object(market_data, "fetch_and_validate_bars", fake_fetch):
            heat = _calculate_portfolio_heat(state, settings)

        # Price is 145, cost is 150 → -$50 loss → heat = 0.05%
        assert heat == 50.0 / 100_000.0

    def test_stop_loss_fallback_on_fetch_failure(self) -> None:
        """When price fetch fails, uses stop-loss (fail-closed)."""
        from trading_bot.data import market_data
        from trading_bot.data.validation import ValidationResult
        import pandas as pd

        positions = {"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0, stop_loss=140.0)}
        state = self._make_state(positions)

        from trading_bot.config.settings import Settings, AppSettings, MarketDataSettings, RiskSettings

        settings = Settings(
            app=AppSettings(state_db_path="state/test.db", log_dir="logs"),
            market_data=MarketDataSettings(intraday_period="5d", intraday_interval="5m"),
            risk=RiskSettings(max_portfolio_heat_pct=0.03),
        )

        def fake_fetch_raises(*a, **k):
            raise RuntimeError("network failure")

        with patch.object(market_data, "fetch_and_validate_bars", fake_fetch_raises):
            heat = _calculate_portfolio_heat(state, settings)

        # Fallback = stop-loss = 140, cost = 150 → -$100 loss → heat = 0.1%
        assert heat == 100.0 / 100_000.0

    def test_stop_loss_fallback_on_validation_failure(self) -> None:
        """When validation fails, uses stop-loss (fail-closed)."""
        from trading_bot.data import market_data
        from trading_bot.data.validation import ValidationResult
        import pandas as pd

        positions = {"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0, stop_loss=140.0)}
        state = self._make_state(positions)

        from trading_bot.config.settings import Settings, AppSettings, MarketDataSettings, RiskSettings

        settings = Settings(
            app=AppSettings(state_db_path="state/test.db", log_dir="logs"),
            market_data=MarketDataSettings(intraday_period="5d", intraday_interval="5m"),
            risk=RiskSettings(max_portfolio_heat_pct=0.03),
        )

        def fake_fetch_bad_data(*a, **k):
            frame = pd.DataFrame({"close": [140.0]})
            return frame, ValidationResult(valid=False, reason="bad data")

        with patch.object(market_data, "fetch_and_validate_bars", fake_fetch_bad_data):
            heat = _calculate_portfolio_heat(state, settings)

        # Fallback = stop-loss = 140 → -$100 loss
        assert heat == 100.0 / 100_000.0

    def test_no_stop_loss_falls_back_to_avg_cost(self) -> None:
        """When no stop-loss is defined, falls back to average_cost."""
        from trading_bot.data import market_data
        from trading_bot.data.validation import ValidationResult

        positions = {"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0, stop_loss=None)}
        state = self._make_state(positions)

        from trading_bot.config.settings import Settings, AppSettings, MarketDataSettings, RiskSettings

        settings = Settings(
            app=AppSettings(state_db_path="state/test.db", log_dir="logs"),
            market_data=MarketDataSettings(intraday_period="5d", intraday_interval="5m"),
            risk=RiskSettings(max_portfolio_heat_pct=0.03),
        )

        def fake_fetch_raises(*a, **k):
            raise RuntimeError("network failure")

        with patch.object(market_data, "fetch_and_validate_bars", fake_fetch_raises):
            heat = _calculate_portfolio_heat(state, settings)

        # No stop-loss → fallback = average_cost = 150 → 0 unrealized P&L
        assert heat == 0.0

    def test_heat_blocks_new_trades_on_data_failure(self) -> None:
        """Heat with stop-loss fallback correctly exceeds max_portfolio_heat_pct."""
        from trading_bot.data import market_data
        from trading_bot.data.validation import ValidationResult

        # Large position: 100 shares at $150, stop at $140 → $1,000 loss if fallback
        positions = {"AAPL": Position(ticker="AAPL", quantity=100, average_cost=150.0, stop_loss=140.0)}
        state = self._make_state(positions)

        from trading_bot.config.settings import Settings, AppSettings, MarketDataSettings, RiskSettings

        settings = Settings(
            app=AppSettings(state_db_path="state/test.db", log_dir="logs"),
            market_data=MarketDataSettings(intraday_period="5d", intraday_interval="5m"),
            risk=RiskSettings(max_portfolio_heat_pct=0.005),  # 0.5% threshold
        )

        def fake_fetch_raises(*a, **k):
            raise RuntimeError("network failure")

        with patch.object(market_data, "fetch_and_validate_bars", fake_fetch_raises):
            heat = _calculate_portfolio_heat(state, settings)

        # Fallback = $140 → -$1,000 loss → heat = 1000/100000 = 1% > 0.5%
        assert heat == 1000.0 / 100_000.0
        assert heat > settings.risk.max_portfolio_heat_pct

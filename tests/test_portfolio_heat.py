"""Tests for portfolio heat fail-closed behaviour (Phase 2)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pandas as pd

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

    def test_mixed_gains_and_losses(self) -> None:
        positions = {
            "AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0),
            "TSLA": Position(ticker="TSLA", quantity=10, average_cost=200.0),
            "MSFT": Position(ticker="MSFT", quantity=10, average_cost=300.0)
        }
        prices = {
            "AAPL": 160.0,  # +$100 gain
            "TSLA": 180.0,  # -$200 loss
            "MSFT": 290.0   # -$100 loss
        }
        # Only losses are counted: 200 + 100 = 300 total loss
        heat = compute_portfolio_heat(positions, prices, 100_000.0)
        assert heat == 300.0 / 100_000.0

    def test_skips_zero_quantity(self) -> None:
        positions = {
            "AAPL": Position(ticker="AAPL", quantity=0, average_cost=150.0)
        }
        prices = {
            "AAPL": 100.0,  # would be -$500 loss if counted (if quantity was 10)
        }
        heat = compute_portfolio_heat(positions, prices, 100_000.0)
        assert heat == 0.0

    def test_negative_equity_returns_zero(self) -> None:
        positions = {"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0)}
        prices = {"AAPL": 140.0}
        heat = compute_portfolio_heat(positions, prices, -10_000.0)
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


class TestRunPaperTradeHeatRecompute:
    """run_paper_trade must recompute portfolio_heat after each fill, not freeze
    it once before the symbol loop. Otherwise the Nth fill's heat check uses
    stale data from before fills 1..N-1."""

    @staticmethod
    def _daily_frame() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
                "open": [100.0 + i for i in range(60)],
                "high": [101.0 + i for i in range(60)],
                "low": [99.0 + i for i in range(60)],
                "close": [100.0 + i for i in range(60)],
                "volume": [1_000_000 for _ in range(60)],
            }
        )

    @staticmethod
    def _intraday_frame() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2026-06-13 10:00:00",
                        "2026-06-13 10:05:00",
                        "2026-06-13 10:10:00",
                        "2026-06-13 10:15:00",
                        "2026-06-13 10:20:00",
                    ]
                ),
                "open": [99.9, 100.1, 100.0, 100.2, 100.5],
                "high": [100.1, 100.3, 100.2, 100.4, 101.1],
                "low": [99.8, 100.0, 99.9, 100.1, 100.4],
                "close": [100.0, 100.2, 100.1, 100.3, 101.0],
                "volume": [1000, 1100, 950, 1050, 2500],
            }
        )

    def test_heat_recomputed_after_fill(self, monkeypatch, tmp_path) -> None:
        import trading_bot.data.market_data as market_data
        import trading_bot.runtime.orchestrator as orchestrator
        from trading_bot.config.loader import load_settings
        from trading_bot.config.settings import AppSettings
        from trading_bot.models.signal import TradeSignal
        from trading_bot.portfolio.ledger import PortfolioLedger
        from trading_bot.runtime.orchestrator import run_paper_trade

        config_file = tmp_path / "config.yaml"
        db_path = tmp_path / "state.db"
        log_dir = tmp_path / "logs"
        config_file.write_text(
            "app:\n"
            f"  state_db_path: {db_path}\n"
            f"  log_dir: {log_dir}\n",
            encoding="utf-8",
        )
        settings = load_settings(config_file)
        # Disable confluence + yellow gates so the signal proceeds to the fill path.
        settings.app.min_entry_confluence_score = 0.0
        settings.app.allow_yellow_mean_reversion = True

        daily = self._daily_frame()
        intraday = self._intraday_frame()

        def fake_fetch_bars(symbol, period, interval, **kwargs):
            assert symbol == "AAPL"
            return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

        monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
        monkeypatch.setattr(
            orchestrator,
            "_scan_now",
            lambda signal_timestamp: datetime(2026, 6, 13, 10, 25, 0, tzinfo=signal_timestamp.tzinfo),
        )

        signal = TradeSignal(
            ticker="AAPL",
            timeframe="intraday",
            action="BUY",
            entry_price=101.0,
            stop_loss=99.8,
            profit_target=103.4,
            risk_reward_ratio=2.0,
            confidence=0.8,
            reasons=["test"],
            timestamp=datetime(2026, 6, 13, 10, 20, 0),
            strategy_tag="test_breakout",
        )
        monkeypatch.setattr(
            orchestrator,
            "_build_signal_result",
            lambda symbol, settings: (signal, "approved", {"daily_close": 100.0, "quality": "GREEN"}),
        )

        call_count = [0]

        def fake_heat(state, settings):
            call_count[0] += 1
            return 0.0

        monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", fake_heat)

        PortfolioLedger(db_path).save_portfolio_state(
            PortfolioState(cash=20_000.0, equity=20_000.0)
        )

        results = run_paper_trade(["AAPL"], settings)
        assert any("FILLED" in r for r in results), (
            f"expected AAPL to fill; got {results!r}"
        )

        # Pre-fix bug: 1 call (line 498 only). Post-fix: >= 2 (line 498 + post-fill).
        assert call_count[0] >= 2, (
            f"portfolio_heat must be recomputed after each fill, "
            f"but _calculate_portfolio_heat was called {call_count[0]} times"
        )

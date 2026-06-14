import pytest
import pandas as pd
from pydantic import ValidationError

from trading_bot.models.market import MarketBar
from trading_bot.models.order import FillResult, OrderRequest
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.models.signal import TradeSignal
from trading_bot.strategy.daily_filter import is_bullish_daily_regime
from trading_bot.strategy.intraday_signal_engine import generate_signal
from trading_bot.strategy.setup_rules import detect_intraday_breakout


def test_daily_regime_true_when_price_above_trend() -> None:
    frame = pd.DataFrame(
        {
            "close": [100, 102, 104],
            "ema_20": [99, 100, 101],
            "sma_50": [98, 99, 100],
        }
    )

    assert is_bullish_daily_regime(frame) is True


def test_daily_regime_false_for_empty_or_missing_columns() -> None:
    assert is_bullish_daily_regime(pd.DataFrame()) is False
    assert is_bullish_daily_regime(pd.DataFrame({"close": [1], "ema_20": [1]})) is False


def test_intraday_breakout_detects_range_break() -> None:
    frame = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "volume": [1000, 1100, 950, 1050, 2500],
            "volume_avg_5": [1000, 1000, 1000, 1000, 1000],
        }
    )

    breakout = detect_intraday_breakout(frame)
    assert breakout is True


def test_intraday_breakout_false_for_missing_columns_or_short_frame() -> None:
    short_frame = pd.DataFrame(
        {
            "close": [100.0, 100.2],
            "high": [100.1, 100.3],
            "volume": [1000, 1100],
            "volume_avg_5": [1000, 1000],
        }
    )
    missing_column_frame = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "volume": [1000, 1100, 950, 1050, 2500],
        }
    )

    assert detect_intraday_breakout(short_frame) is False
    assert detect_intraday_breakout(missing_column_frame) is False


def test_generate_signal_returns_buy_candidate_on_bullish_breakout() -> None:
    daily = pd.DataFrame(
        {
            "close": [100.0, 102.0, 104.0],
            "ema_20": [99.0, 100.0, 101.0],
            "sma_50": [98.0, 99.0, 100.0],
        },
        index=pd.to_datetime(["2026-06-13 09:30:00", "2026-06-13 09:35:00", "2026-06-13 09:40:00"]),
    )
    intraday = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "volume": [1000, 1100, 950, 1050, 2500],
            "volume_avg_5": [1000, 1000, 1000, 1000, 1000],
            "low": [99.8, 100.0, 99.91234, 100.1, 100.4],
        },
        index=pd.to_datetime(
            [
                "2026-06-13 10:00:00",
                "2026-06-13 10:05:00",
                "2026-06-13 10:10:00",
                "2026-06-13 10:15:00",
                "2026-06-13 10:20:00",
            ]
        ),
    )

    signal = generate_signal("AAPL", daily, intraday)

    assert signal is not None
    assert isinstance(signal, TradeSignal)
    assert signal.ticker == "AAPL"
    assert signal.action == "BUY"
    assert signal.timeframe == "intraday"
    assert signal.entry_price > signal.stop_loss
    assert signal.profit_target > signal.entry_price
    assert signal.risk_reward_ratio == pytest.approx(
        round((signal.profit_target - signal.entry_price) / (signal.entry_price - signal.stop_loss), 6)
    )
    assert "bullish daily regime" in signal.reasons
    assert "intraday breakout" in signal.reasons
    assert signal.timestamp == pd.Timestamp("2026-06-13 10:20:00")


def test_generate_signal_returns_none_without_datetime_index() -> None:
    daily = pd.DataFrame(
        {
            "close": [100.0, 102.0, 104.0],
            "ema_20": [99.0, 100.0, 101.0],
            "sma_50": [98.0, 99.0, 100.0],
        }
    )
    intraday = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "volume": [1000, 1100, 950, 1050, 2500],
            "volume_avg_5": [1000, 1000, 1000, 1000, 1000],
            "low": [99.8, 100.0, 99.9, 100.1, 100.4],
        }
    )

    assert generate_signal("AAPL", daily, intraday) is None


def test_trade_signal_requires_stop_loss_and_core_fields() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=102.5,
        risk_reward_ratio=2.5,
        confidence=0.75,
        reasons=["breakout"],
        strategy_tag="opening-range-breakout",
        timestamp="2026-06-13T10:00:00-04:00",
    )

    assert signal.ticker == "AAPL"
    assert signal.stop_loss == 99.0
    assert signal.risk_reward_ratio == 2.5
    assert signal.reasons == ["breakout"]


def test_trade_signal_rejects_buy_setups_with_incoherent_prices() -> None:
    with pytest.raises(ValidationError, match="BUY trade requires"):
        TradeSignal(
            ticker="AAPL",
            timeframe="intraday",
            action="BUY",
            entry_price=100.0,
            stop_loss=101.0,
            profit_target=102.5,
            risk_reward_ratio=2.5,
            confidence=0.75,
            reasons=["breakout"],
            strategy_tag="opening-range-breakout",
            timestamp="2026-06-13T10:00:00-04:00",
        )


def test_order_request_rejects_missing_price_requirements() -> None:
    with pytest.raises(ValidationError, match="limit_price"):
        OrderRequest(
            ticker="AAPL",
            side="BUY",
            order_type="limit",
            quantity=10,
            submitted_at="2026-06-13T10:00:00-04:00",
        )

    with pytest.raises(ValidationError, match="stop_price"):
        OrderRequest(
            ticker="AAPL",
            side="BUY",
            order_type="stop",
            quantity=10,
            submitted_at="2026-06-13T10:00:00-04:00",
        )

    with pytest.raises(ValidationError, match="limit_price.*stop_price"):
        OrderRequest(
            ticker="AAPL",
            side="BUY",
            order_type="bracket",
            quantity=10,
            submitted_at="2026-06-13T10:00:00-04:00",
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"high": 9.5, "low": 9.0, "open": 10.0, "close": 9.8},
        {"high": 10.5, "low": 10.1, "open": 10.0, "close": 10.2},
    ],
)
def test_market_bar_rejects_impossible_ohlc_ranges(fields: dict[str, float]) -> None:
    with pytest.raises(ValidationError, match="OHLC"):
        MarketBar(
            ticker="AAPL",
            timeframe="intraday",
            timestamp="2026-06-13T10:00:00-04:00",
            volume=1_000,
            **fields,
        )


def test_fill_result_requires_positive_quantity() -> None:
    with pytest.raises(ValidationError, match="quantity"):
        FillResult(
            order_id="order-1",
            ticker="AAPL",
            quantity=0,
            fill_price=100.0,
            fees=1.0,
            filled_at="2026-06-13T10:00:00-04:00",
        )


def test_portfolio_state_rejects_negative_cash_and_equity() -> None:
    with pytest.raises(ValidationError, match="cash"):
        PortfolioState(
            cash=-1.0,
            equity=1000.0,
            positions={"AAPL": Position(ticker="AAPL", quantity=1, average_cost=100.0)},
        )

    with pytest.raises(ValidationError, match="equity"):
        PortfolioState(
            cash=1000.0,
            equity=-1.0,
            positions={"AAPL": Position(ticker="AAPL", quantity=1, average_cost=100.0)},
        )

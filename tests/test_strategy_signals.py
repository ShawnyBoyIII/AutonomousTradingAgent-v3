from trading_bot.models.signal import TradeSignal


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

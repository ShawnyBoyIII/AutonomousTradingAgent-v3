from trading_bot.risk.exposure import exceeds_ticker_allocation
from trading_bot.risk.position_sizer import calculate_position_size


def test_calculate_position_size_uses_account_risk() -> None:
    shares = calculate_position_size(
        account_equity=10000,
        risk_pct=0.01,
        entry_price=100,
        stop_loss=99,
    )
    assert shares == 100


def test_exceeds_ticker_allocation_flags_over_limit_position() -> None:
    assert exceeds_ticker_allocation(
        account_equity=10000,
        position_value=2500,
        max_allocation_pct=0.2,
    )

from trading_bot.risk.exposure import exceeds_ticker_allocation
from trading_bot.risk.position_sizer import calculate_fixed_stop_position_size


def test_calculate_position_size_uses_account_risk() -> None:
    shares = calculate_fixed_stop_position_size(
        account_equity=10000,
        risk_pct=0.01,
        entry_price=100,
        stop_loss=99,
        max_position_pct=1.0,  # No cap for this test
    )
    assert shares == 100


def test_calculate_position_size_handles_decimal_risk_per_share() -> None:
    # Use realistic numbers: $0.10 risk per share, want 100 shares
    shares = calculate_fixed_stop_position_size(
        account_equity=10000,
        risk_pct=0.10,  # 10% risk = $1k
        entry_price=100,
        stop_loss=99.9,  # $0.10 risk per share
        max_position_pct=1.0,  # No cap for this test
    )
    # $1k risk / $0.10 per share = 10,000 shares
    # But position value = 10,000 × $100 = $1M > $10k equity
    # So it should be capped by max_position_value = $10k / $100 = 100 shares
    assert shares == 100


def test_exceeds_ticker_allocation_flags_over_limit_position() -> None:
    assert exceeds_ticker_allocation(
        account_equity=10000,
        position_value=2500,
        max_allocation_pct=0.2,
    )


def test_exceeds_ticker_allocation_allows_at_limit_position() -> None:
    assert (
        exceeds_ticker_allocation(
            account_equity=10000,
            position_value=2000,
            max_allocation_pct=0.2,
        )
        is False
    )

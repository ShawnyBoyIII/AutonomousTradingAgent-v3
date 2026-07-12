from __future__ import annotations

import pandas as pd

from trading_bot.backtest import runner
from trading_bot.config.settings import Settings


def test_intraday_backtest_uses_configured_paper_execution_costs(monkeypatch) -> None:
    observed: list[dict[str, object]] = []

    class FakeBroker:
        def __init__(self, **kwargs):
            observed.append(kwargs)
            self.positions = {}

    monkeypatch.setattr(runner, "PaperBroker", FakeBroker)
    settings = Settings()
    settings.paper.fee_per_order = 2.5
    settings.paper.slippage_bps = 7
    settings.paper.dynamic_slippage_enabled = True
    settings.paper.dynamic_slippage_notional_bps_per_10k = 3.0
    settings.paper.dynamic_slippage_low_price_boost_bps = 8.0
    settings.paper.dynamic_slippage_max_extra_bps = 30.0
    daily = pd.DataFrame({"close": [100.0]})
    intraday = pd.DataFrame({"close": [100.0] * 5})

    runner._run_symbol_backtest("AAPL", daily, intraday, settings)

    assert observed == [
        {
            "starting_cash": 10_000.0,
            "fee_per_order": 2.5,
            "slippage_bps": 7,
            "dynamic_slippage_enabled": True,
            "dynamic_slippage_notional_bps_per_10k": 3.0,
            "dynamic_slippage_low_price_boost_bps": 8.0,
            "dynamic_slippage_max_extra_bps": 30.0,
        }
    ]

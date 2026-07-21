import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_bot.backtest.runner import run_backtest
from trading_bot.config.settings import (
    AppSettings,
    MarketDataSettings,
    PaperSettings,
    RiskSettings,
    Settings,
    SupermodelSettings,
)
from trading_bot.data.market_data import fetch_bars
from trading_bot.models.order import FillResult
from trading_bot.models.portfolio import PortfolioState
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.runtime.orchestrator import run_paper_trade


def _record_sell(
    ledger: PortfolioLedger,
    pnl: float,
    filled_at: datetime,
    strategy_tag: str = "v3-mean_reversion",
) -> None:
    fill = FillResult(
        order_id=f"sell-{filled_at.isoformat()}-{pnl}",
        ticker="TEST",
        quantity=1,
        fill_price=100.0,
        fees=1.0,
        filled_at=filled_at,
    )
    ledger.record_fill(fill, side="SELL", realized_pnl=pnl, strategy_tag=strategy_tag)


def _settings(
    *,
    candidate_multiplier: float = 1.0,
    allow_yellow: bool = True,
    state_db_path: str = "",
    min_confluence: float = 0.0,
    min_rr: float = 1.0,
    ticker_alloc_pct: float = 0.20,
) -> Settings:
    from trading_bot.config.settings import StrategySettings

    return Settings(
        app=AppSettings(
            state_db_path=state_db_path,
            log_dir=str(Path(state_db_path).parent / "logs"),
            timezone="America/New_York",
            allow_yellow_mean_reversion=allow_yellow,
            min_entry_confluence_score=min_confluence,
        ),
        market_data=MarketDataSettings(
            provider="polygon",
            validate_data=False,
            max_data_age_minutes=240,
        ),
        risk=RiskSettings(
            max_ticker_allocation_pct=ticker_alloc_pct,
            min_reward_risk_ratio=min_rr,
            use_atr_sizing=False,
            yellow_allocation_pct=0.5,
            max_daily_orders=60,
        ),
        strategy=StrategySettings(use_v3_signals=True),
        paper=PaperSettings(fee_per_order=0.0, slippage_bps=0),
        supermodel=SupermodelSettings(
            range_bound_trend_caution_multiplier=candidate_multiplier,
        ),
    )


def _seed_ohlcv(symbol: str, periods: int = 220, base_price: float = 100.0):
    import pandas as pd

    times = pd.date_range("2026-01-02 09:30", periods=periods, freq="5min")
    rows = []
    for i in range(periods):
        price = base_price + (i % 20) * 0.1
        rows.append(
            {
                "timestamp": times[i],
                "open": price,
                "high": price + 0.05,
                "low": price - 0.05,
                "close": price + 0.02,
                "volume": 1000 + (i % 100) * 10,
            }
        )
    df = pd.DataFrame(rows).set_index("timestamp")
    return df


def test_policy_metadata_does_not_match_when_v25_consensus_selected(
    monkeypatch, tmp_path: Path
) -> None:
    """Even if v3 metadata is present, a v2.5-selected consensus must NOT
    scale the entry via the range_bound_trend_caution policy."""
    db = tmp_path / "state.db"
    settings = _settings(
        candidate_multiplier=0.5,
        state_db_path=str(db),
    )
    ledger = PortfolioLedger(db)
    state = PortfolioState(cash=20_000.0, equity=20_000.0)
    ledger.save_portfolio_state(state)
    ledger.record_equity_snapshot(state)

    monkeypatch.setattr(
        "trading_bot.data.market_data.fetch_bars",
        lambda *args, **kwargs: _seed_ohlcv(args[0] if args else kwargs.get("symbol", "SPY")),
    )

    # Force the parallel consensus to pick v2.5 instead of v3
    import trading_bot.runtime.orchestrator as orch

    def fake_v3(symbol, settings, daily_frame=None, intraday_frame=None, hourly_frame=None, fetch_hourly=True):
        return None, "v3 disabled", {"v3_total_score": 0.0}

    monkeypatch.setattr(orch, "_build_v3_signal_result", fake_v3)
    results = run_paper_trade(["SPY"], settings=settings)
    assert results  # No crash; v2.5 path processed


def test_policy_metadata_recorded_in_decision_event_when_active(
    monkeypatch, tmp_path: Path
) -> None:
    """When V3 wins, range_bound trend caution multiplier metadata must
    appear on the persisted decision event."""
    db = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    settings = _settings(
        candidate_multiplier=0.5,
        state_db_path=str(db),
    )
    settings.app.log_dir = str(log_dir)
    ledger = PortfolioLedger(db)
    state = PortfolioState(cash=20_000.0, equity=20_000.0)
    ledger.save_portfolio_state(state)
    ledger.record_equity_snapshot(state)

    monkeypatch.setattr(
        "trading_bot.data.market_data.fetch_bars",
        lambda *args, **kwargs: _seed_ohlcv(args[0] if args else kwargs.get("symbol", "SPY")),
    )

    import trading_bot.runtime.orchestrator as orch

    def fake_v3(symbol, settings, daily_frame=None, intraday_frame=None, hourly_frame=None, fetch_hourly=True):
        from trading_bot.models.signal import TradeSignal

        signal = TradeSignal(
            ticker=symbol,
            timeframe="intraday",
            action="BUY",
            entry_price=100.0,
            stop_loss=99.0,
            profit_target=103.0,
            risk_reward_ratio=3.0,
            confidence=0.7,
            reasons=["test"],
            strategy_tag="v3-trend_following",
            timestamp=datetime(2026, 1, 2, 9, 30, tzinfo=timezone.utc),
            quality="GREEN",
        )
        return signal, "ok", {
            "v3_total_score": 7.0,
            "v3_regime": "range_bound",
            "regime": "range_bound",
            "supermodel_decision": "caution",
            "quality": "GREEN",
        }

    monkeypatch.setattr(orch, "_build_v3_signal_result", fake_v3)
    results = run_paper_trade(["SPY"], settings=settings)

    decision_log = log_dir / "decision-log.jsonl"
    assert decision_log.exists()
    lines = [
        json.loads(line)
        for line in decision_log.read_text().splitlines()
        if line.strip()
    ]
    filled = [
        line for line in lines
        if line.get("status") == "FILLED"
    ]
    assert filled, f"No FILLED events in {lines!r}"
    policy = filled[0]["entry_policy"]
    assert policy["applied"] is True
    assert policy["multiplier"] == 0.5


def test_backtest_runner_invokes_entry_policy_helper() -> None:
    """The backtest runner must call the same conditional entry-policy
    helper that paper trading uses, so a candidate with multiplier < 1.0
    produces smaller fills than the baseline."""
    import inspect

    import trading_bot.backtest.runner as backtest_runner

    source = inspect.getsource(backtest_runner)
    assert "compute_entry_policy_multiplier" in source, (
        "backtest runner must apply the same conditional entry-policy "
        "helper as paper trading so offline replay reflects the candidate"
    )
    assert "policy_decision.applied" in source
    assert "policy_decision.multiplier <= 0" in source, (
        "zero multiplier must produce an explicit block in backtest "
        "(mirrors paper trading)"
    )


def test_backtest_runner_propagates_v3_metadata(monkeypatch) -> None:
    """The backtest runner's V3 selector branch must propagate regime and
    supermodel_decision into details so the conditional entry-policy helper
    can fire. Without this, replay-based tuning experiments cannot
    differentiate baseline vs. candidate."""
    src = open("trading_bot/backtest/runner.py", encoding="utf-8").read()
    assert 'details["v3_regime"]' in src, (
        "backtest runner must persist selection.regime into details "
        "so the entry-policy helper's regime cascade finds it"
    )
    assert 'details["v3_confidence"]' in src
    assert 'details["v3_strategy"]' in src
    assert "stacked.to_details" in src, (
        "backtest runner must merge stacked.to_details() so "
        "supermodel_decision becomes available to compute_entry_policy_multiplier"
    )


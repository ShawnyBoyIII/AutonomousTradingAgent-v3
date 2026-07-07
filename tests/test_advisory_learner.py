from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from trading_bot.config.settings import AppSettings, AdvisorySettings, Settings
from trading_bot.db.repositories.scan_features import upsert_scan_feature
from trading_bot.db.repositories.trades import upsert_trade
from trading_bot.db.session import get_session, init_db, make_session_factory
from trading_bot.models.order import FillResult
from trading_bot.portfolio.ledger import PortfolioLedger


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app=AppSettings(
            state_db_path=str(tmp_path / "state" / "burn_in.db"),
            log_dir=str(tmp_path / "logs"),
            advisory_dir=str(tmp_path / "state" / "advisory"),
            universe_candidates_path=str(tmp_path / "state" / "universe_candidates.json"),
        ),
        advisory=AdvisorySettings(
            enabled=True,
            min_observations_per_symbol=1,
            main_limit=5,
            cheap_limit=5,
            cheap_stock_max_price=5.0,
            min_hit_rate_for_promote=0.5,
        ),
    )


def test_run_advisory_learner_writes_artifacts_and_daily_report(tmp_path: Path, monkeypatch) -> None:
    from trading_bot.advisory.learner import run_advisory_learner

    settings = _settings(tmp_path)
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "command": "scan",
                        "ticker": "AAPL",
                        "status": "APPROVED",
                        "reason": "approved",
                        "confidence": 0.9,
                        "quality": "GREEN",
                        "entry_price": 100.0,
                        "supermodel_decision": "support",
                    }
                ),
                json.dumps(
                    {
                        "command": "scan",
                        "ticker": "XYZ",
                        "status": "REJECTED",
                        "reason": "stale market data",
                        "confidence": 0.2,
                        "quality": "RED",
                        "entry_price": 12.0,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    Path(settings.app.universe_candidates_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.app.universe_candidates_path).write_text(
        json.dumps(
            {
                "mode": "universe",
                "candidates": [
                    {
                        "ticker": "AAPL",
                        "included": True,
                        "rank": 1,
                        "source_names": ["small_cap_gainers"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    ledger = PortfolioLedger(Path(settings.app.state_db_path))
    ledger.record_fill(
        FillResult(
            order_id="buy-aapl",
            ticker="AAPL",
            quantity=10,
            fill_price=100.0,
            fees=1.0,
            filled_at=datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc),
        ),
        side="BUY",
        strategy_tag="v3-trend_following|stack:support",
    )
    ledger.record_fill(
        FillResult(
            order_id="sell-aapl",
            ticker="AAPL",
            quantity=10,
            fill_price=110.0,
            fees=1.0,
            filled_at=datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc),
        ),
        side="SELL",
        realized_pnl=98.0,
        strategy_tag="v3-trend_following|stack:support",
    )

    import trading_bot.data.market_data as market_data

    monkeypatch.setattr(
        market_data,
        "fetch_small_cap_candidates",
        lambda limit=200, screeners=None: [
            {
                "symbol": "CHEAP",
                "quoteType": "EQUITY",
                "exchange": "NYQ",
                "marketCap": 3_000_000_000,
                "regularMarketPrice": 2.5,
                "averageDailyVolume3Month": 800_000,
                "dayVolume": 1_800_000,
                "source": "small_cap_gainers",
            },
            {
                "symbol": "PENNY",
                "quoteType": "EQUITY",
                "exchange": "NYQ",
                "marketCap": 3_000_000_000,
                "regularMarketPrice": 1.5,
                "averageDailyVolume3Month": 200_000,
                "dayVolume": 300_000,
                "source": "aggressive_small_caps",
            },
        ],
    )

    summary = run_advisory_learner(settings, write_daily_report=True)

    advisory_dir = Path(settings.app.advisory_dir)
    assert summary.observations_added == 2
    assert (advisory_dir / "observations.jsonl").exists()
    assert (advisory_dir / "latest_report.json").exists()
    assert (advisory_dir / "Daily report.md").exists()

    report = json.loads((advisory_dir / "latest_report.json").read_text(encoding="utf-8"))
    main_rows = {"recommendations": report["main_midcap"]}
    cheap_rows = {"recommendations": report["cheap_stocks"]}
    override = yaml.safe_load((advisory_dir / "scout_override.yaml").read_text(encoding="utf-8"))

    assert any(row["ticker"] == "AAPL" for row in main_rows["recommendations"])
    assert any(row["ticker"] == "CHEAP" for row in cheap_rows["recommendations"])
    assert "AAPL" in override["main_midcap"]["promote_symbols"]
    assert "CHEAP" in override["cheap_stocks"]["separate_watchlist"]


def test_run_advisory_learner_resumes_from_saved_offset(tmp_path: Path) -> None:
    from trading_bot.advisory.learner import run_advisory_learner

    settings = _settings(tmp_path)
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(
            {
                "command": "scan",
                "ticker": "AAPL",
                "status": "APPROVED",
                "reason": "approved",
                "confidence": 0.8,
                "quality": "GREEN",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    first = run_advisory_learner(settings)
    second = run_advisory_learner(settings)

    assert first.observations_added == 1
    assert second.observations_added == 0


def test_run_advisory_learner_disabled_is_noop(tmp_path: Path) -> None:
    from trading_bot.advisory.learner import advisory_paths, run_advisory_learner

    settings = _settings(tmp_path)
    settings.advisory.enabled = False
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps({"command": "scan", "ticker": "AAPL", "status": "APPROVED"}) + "\n",
        encoding="utf-8",
    )

    summary = run_advisory_learner(settings)

    assert summary.observations_added == 0
    assert not advisory_paths(settings).latest_report.exists()


def test_run_advisory_learner_uses_scan_features_and_closed_trades_for_scoring(tmp_path: Path) -> None:
    from trading_bot.advisory.learner import run_advisory_learner

    settings = _settings(tmp_path)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        upsert_scan_feature(
            session,
            ticker="AAPL",
            status="APPROVED",
            action="BUY",
            confidence=0.92,
            quality="GREEN",
            supermodel_score=0.8,
            v3_total_score=8.0,
        )
        upsert_scan_feature(
            session,
            ticker="AAPL",
            status="APPROVED",
            action="BUY",
            confidence=0.88,
            quality="GREEN",
            supermodel_score=0.75,
            v3_total_score=7.5,
        )
        upsert_trade(
            session,
            ticker="AAPL",
            side="BUY",
            order_type="market",
            quantity=10,
            entry_price=100.0,
            strategy_tag="v3-trend_following|stack:support",
        )
        trade = upsert_trade(
            session,
            ticker="AAPL",
            side="BUY",
            order_type="market",
            quantity=10,
            entry_price=100.0,
            strategy_tag="v3-trend_following|stack:support",
        )
        trade.status = "CLOSED"
        trade.pnl = 120.0
        session.commit()
    finally:
        session.close()
        engine.dispose()

    summary = run_advisory_learner(settings)

    assert summary.observations_added == 0
    report = json.loads((Path(settings.app.advisory_dir) / "latest_report.json").read_text(encoding="utf-8"))
    main_rows = {"recommendations": report["main_midcap"]}
    aapl = next(row for row in main_rows["recommendations"] if row["ticker"] == "AAPL")
    assert aapl["approval_rate"] > 0.9
    assert aapl["win_rate"] == 1.0
    assert aapl["net_pnl"] == 120.0

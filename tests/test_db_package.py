"""Tests for the trading_bot.db package (session, models, repositories).

All tests use in-memory SQLite — no network, no filesystem, deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from trading_bot.db.models import (
    Base,
    Event,
    MarketData,
    ModelPrediction,
    PortfolioSnapshot,
    Position,
    ScanFeature,
    ScanResult,
    Trade,
)
from trading_bot.db.repositories import (
    close_position,
    create_snapshot,
    get_events,
    get_latest_bar_timestamp,
    get_market_bars,
    get_open_positions,
    get_open_trades,
    get_predictions,
    get_scan_features,
    get_scan_results,
    get_snapshots,
    get_trades,
    is_market_data_stale,
    log_event,
    update_trade_exit,
    upsert_market_bars,
    upsert_position,
    upsert_prediction,
    upsert_scan_result,
    upsert_scan_feature,
    upsert_trade,
)


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def session(session_factory):
    sess = session_factory()
    yield sess
    sess.close()


# ──────────────────────────── Session tests ────────────────────────────


class TestSession:
    def test_init_db_creates_all_tables(self, tmp_path):
        from trading_bot.config.settings import AppSettings, Settings

        settings = Settings(
            app=AppSettings(
                state_db_path=str(tmp_path / "state" / "test.db"),
                log_dir=str(tmp_path / "logs"),
                portfolio_summary_path=str(tmp_path / "portfolio.json"),
                scan_results_path=str(tmp_path / "scan.json"),
            )
        )
        from trading_bot.db.session import init_db, make_session_factory, get_session

        eng = init_db(settings)
        assert eng is not None

        factory = make_session_factory(eng)
        assert factory is not None

        sess = get_session(factory)
        assert isinstance(sess, Session)
        sess.close()


# ──────────────────────────── Model tests ────────────────────────────


class TestModels:
    def test_all_tables_created(self, engine):
        insp = __import__("sqlalchemy").inspect(engine)
        tables = set(insp.get_table_names())
        expected = {
            "market_data",
            "scan_results",
            "scan_features",
            "trades",
            "positions",
            "portfolio_snapshots",
            "model_predictions",
            "events",
        }
        assert expected.issubset(tables)

    def test_market_data_columns(self, engine):
        insp = __import__("sqlalchemy").inspect(engine)
        cols = {c["name"] for c in insp.get_columns("market_data")}
        expected = {"id", "ticker", "timeframe", "timestamp", "open", "high", "low", "close", "volume", "fetched_at"}
        assert expected.issubset(cols)

    def test_trade_columns(self, engine):
        insp = __import__("sqlalchemy").inspect(engine)
        cols = {c["name"] for c in insp.get_columns("trades")}
        expected = {
            "id", "ticker", "side", "order_type", "quantity", "entry_price",
            "stop_loss", "profit_target", "fees", "filled_at", "strategy_tag",
            "swarm_sentiment_bucket", "signal_quality", "market_regime", "supermodel_decision",
            "swarm_decision", "consensus", "swarm_sentiment_score", "swarm_sentiment_confidence",
            "entry_volume_ratio", "entry_range_ratio", "adaptive_rr",
            "status", "exit_price", "exit_fees", "exited_at", "pnl",
        }
        assert expected.issubset(cols)

    def test_scan_feature_columns(self, engine):
        insp = __import__("sqlalchemy").inspect(engine)
        cols = {c["name"] for c in insp.get_columns("scan_features")}
        expected = {
            "id", "scan_result_id", "ticker", "timestamp", "status", "action", "confidence",
            "quality", "freshness", "market_age_minutes", "market_regime", "strategy_tag",
            "consensus", "v3_total_score", "supermodel_score", "swarm_confidence",
            "swarm_sentiment_score", "swarm_sentiment_confidence", "mtf_aligned",
            "entry_volume_ratio", "entry_range_ratio", "adaptive_rr",
        }
        assert expected.issubset(cols)

    def test_position_columns(self, engine):
        insp = __import__("sqlalchemy").inspect(engine)
        cols = {c["name"] for c in insp.get_columns("positions")}
        expected = {
            "id", "ticker", "quantity", "average_cost", "stop_loss",
            "profit_target", "highest_high", "entry_at", "strategy_tag", "closed_at",
        }
        assert expected.issubset(cols)


# ──────────────────────────── Market data repository ────────────────────────────


class TestMarketDataRepo:
    def _make_bars(self, n: int = 3, start: datetime | None = None) -> pd.DataFrame:
        if start is None:
            start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        return pd.DataFrame({
            "timestamp": [start + timedelta(hours=i) for i in range(n)],
            "open": [100.0 + i for i in range(n)],
            "high": [102.0 + i for i in range(n)],
            "low": [98.0 + i for i in range(n)],
            "close": [101.0 + i for i in range(n)],
            "volume": [1000 + i * 100 for i in range(n)],
        })

    def test_upsert_inserts_new_bars(self, session):
        bars = self._make_bars(3)
        count = upsert_market_bars(session, "AAPL", "5m", bars)
        assert count == 3

    def test_upsert_updates_existing_bars(self, session):
        bars = self._make_bars(3)
        upsert_market_bars(session, "AAPL", "5m", bars)

        bars["close"] = [200.0, 201.0, 202.0]
        upsert_market_bars(session, "AAPL", "5m", bars)

        result = get_market_bars(session, "AAPL", "5m")
        assert list(result["close"]) == [200.0, 201.0, 202.0]

    def test_get_market_bars_empty(self, session):
        result = get_market_bars(session, "NOPE", "5m")
        assert result.empty

    def test_get_market_bars_with_limit(self, session):
        bars = self._make_bars(5)
        upsert_market_bars(session, "AAPL", "5m", bars)
        result = get_market_bars(session, "AAPL", "5m", limit=2)
        assert len(result) == 2

    def test_get_market_bars_with_since(self, session):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        bars = self._make_bars(5, start)
        upsert_market_bars(session, "AAPL", "5m", bars)
        result = get_market_bars(session, "AAPL", "5m", since=start + timedelta(hours=2))
        assert len(result) == 3

    def test_get_latest_bar_timestamp(self, session):
        bars = self._make_bars(3)
        upsert_market_bars(session, "AAPL", "5m", bars)
        latest = get_latest_bar_timestamp(session, "AAPL", "5m")
        assert latest is not None
        expected = bars["timestamp"].iloc[-1].to_pydatetime()
        # SQLite strips tzinfo, so compare naive values
        assert latest.replace(tzinfo=None) == expected.replace(tzinfo=None)

    def test_get_latest_bar_timestamp_none(self, session):
        assert get_latest_bar_timestamp(session, "NOPE", "5m") is None

    def test_is_market_data_stale_no_data(self, session):
        assert is_market_data_stale(session, "NOPE", "5m") is True

    def test_is_market_data_stale_recent(self, session):
        bars = pd.DataFrame({
            "timestamp": [datetime.now(timezone.utc) - timedelta(minutes=5)],
            "open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5], "volume": [1000],
        })
        upsert_market_bars(session, "AAPL", "5m", bars)
        assert is_market_data_stale(session, "AAPL", "5m", max_age_minutes=30) is False

    def test_is_market_data_stale_old(self, session):
        bars = pd.DataFrame({
            "timestamp": [datetime.now(timezone.utc) - timedelta(hours=2)],
            "open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5], "volume": [1000],
        })
        upsert_market_bars(session, "AAPL", "5m", bars)
        assert is_market_data_stale(session, "AAPL", "5m", max_age_minutes=30) is True

    def test_upsert_string_timestamp(self, session):
        bars = pd.DataFrame({
            "timestamp": ["2025-01-01T10:00:00"],
            "open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5], "volume": [1000],
        })
        upsert_market_bars(session, "AAPL", "5m", bars)
        result = get_market_bars(session, "AAPL", "5m")
        assert len(result) == 1


# ──────────────────────────── Trades repository ────────────────────────────


class TestTradesRepo:
    def test_upsert_trade(self, session):
        trade = upsert_trade(
            session, ticker="AAPL", side="BUY", order_type="market",
            quantity=100, entry_price=150.0, fees=1.0,
        )
        assert trade.id is not None
        assert trade.ticker == "AAPL"
        assert trade.status == "FILLED"

    def test_upsert_trade_with_optional_fields(self, session):
        trade = upsert_trade(
            session, ticker="AAPL", side="BUY", order_type="limit",
            quantity=50, entry_price=145.0, stop_loss=140.0,
            profit_target=160.0, fees=1.0, strategy_tag="momentum", swarm_sentiment_bucket="bullish",
            signal_quality="GREEN", market_regime="strong_uptrend", supermodel_decision="support",
            swarm_decision="APPROVE", consensus="BUY", swarm_sentiment_score=0.42,
            swarm_sentiment_confidence=0.72, entry_volume_ratio=1.35, entry_range_ratio=0.8,
            adaptive_rr=2.5,
        )
        assert trade.stop_loss == 140.0
        assert trade.profit_target == 160.0
        assert trade.strategy_tag == "momentum"
        assert trade.swarm_sentiment_bucket == "bullish"
        assert trade.signal_quality == "GREEN"
        assert trade.market_regime == "strong_uptrend"
        assert trade.swarm_sentiment_score == 0.42
        assert trade.adaptive_rr == 2.5

    def test_update_trade_exit(self, session):
        trade = upsert_trade(session, ticker="AAPL", side="BUY", order_type="market", quantity=100, entry_price=150.0)
        updated = update_trade_exit(session, trade.id, exit_price=160.0, exit_fees=1.0, pnl=900.0)
        assert updated.exit_price == 160.0
        assert updated.pnl == 900.0
        assert updated.status == "CLOSED"
        assert updated.exited_at is not None

    def test_update_trade_exit_with_context(self, session):
        trade = upsert_trade(session, ticker="AAPL", side="BUY", order_type="market", quantity=100, entry_price=150.0)
        updated = update_trade_exit(
            session, trade.id, exit_price=160.0, exit_fees=1.0, pnl=900.0,
            exit_rsi=45.5, exit_atr=2.3, hold_duration_minutes=120.0,
            exit_regime="strong_uptrend", exit_strategy="v3-trend_following",
            exit_reason="profit_target",
        )
        assert updated.exit_rsi == 45.5
        assert updated.exit_atr == 2.3
        assert updated.hold_duration_minutes == 120.0
        assert updated.exit_regime == "strong_uptrend"
        assert updated.exit_strategy == "v3-trend_following"
        assert updated.exit_reason == "profit_target"

    def test_update_trade_exit_not_found(self, session):
        with pytest.raises(ValueError, match="Trade 999 not found"):
            update_trade_exit(session, 999, exit_price=160.0)

    def test_get_open_trades(self, session):
        upsert_trade(session, ticker="AAPL", side="BUY", order_type="market", quantity=100, entry_price=150.0)
        upsert_trade(session, ticker="GOOGL", side="BUY", order_type="market", quantity=50, entry_price=90.0)
        open_trades = get_open_trades(session)
        assert len(open_trades) == 2

    def test_get_open_trades_excludes_closed(self, session):
        trade = upsert_trade(session, ticker="AAPL", side="BUY", order_type="market", quantity=100, entry_price=150.0)
        update_trade_exit(session, trade.id, exit_price=160.0, pnl=900.0)
        open_trades = get_open_trades(session)
        assert len(open_trades) == 0

    def test_get_trades_by_ticker(self, session):
        upsert_trade(session, ticker="AAPL", side="BUY", order_type="market", quantity=100, entry_price=150.0)
        upsert_trade(session, ticker="GOOGL", side="BUY", order_type="market", quantity=50, entry_price=90.0)
        result = get_trades(session, ticker="AAPL")
        assert len(result) == 1
        assert result[0].ticker == "AAPL"

    def test_get_trades_with_limit(self, session):
        for i in range(5):
            upsert_trade(session, ticker="AAPL", side="BUY", order_type="market", quantity=100, entry_price=150.0 + i)
        result = get_trades(session, limit=3)
        assert len(result) == 3

    def test_get_trades_by_swarm_sentiment_bucket(self, session):
        upsert_trade(
            session,
            ticker="AAPL",
            side="BUY",
            order_type="market",
            quantity=100,
            entry_price=150.0,
            swarm_sentiment_bucket="bullish",
        )
        upsert_trade(
            session,
            ticker="MSFT",
            side="BUY",
            order_type="market",
            quantity=50,
            entry_price=200.0,
            swarm_sentiment_bucket="bearish",
        )

        result = get_trades(session, swarm_sentiment_bucket="bullish")

        assert len(result) == 1
        assert result[0].ticker == "AAPL"


class TestScanFeaturesRepo:
    def test_upsert_scan_feature(self, session):
        feature = upsert_scan_feature(
            session,
            ticker="AAPL",
            status="APPROVED",
            action="BUY",
            confidence=0.9,
            quality="GREEN",
            freshness="fresh",
            market_age_minutes=5,
            market_regime="strong_uptrend",
            strategy_tag="v3-trend_following",
            consensus="BUY",
            v3_total_score=8.5,
            supermodel_score=0.7,
            swarm_confidence=0.8,
            swarm_sentiment_score=0.42,
            swarm_sentiment_confidence=0.72,
            mtf_aligned=3,
            entry_volume_ratio=1.4,
            entry_range_ratio=0.9,
            adaptive_rr=2.5,
        )

        assert feature.id is not None
        assert feature.ticker == "AAPL"
        assert feature.swarm_sentiment_score == 0.42

    def test_get_scan_features(self, session):
        upsert_scan_feature(session, ticker="AAPL", status="APPROVED", action="BUY", swarm_sentiment_score=0.5)
        upsert_scan_feature(session, ticker="MSFT", status="REJECTED", action="HOLD", swarm_sentiment_score=-0.4)

        result = get_scan_features(session, ticker="AAPL")

        assert len(result) == 1
        assert result[0].ticker == "AAPL"

    def test_runtime_persists_scan_features(self, tmp_path):
        from trading_bot.config.settings import AppSettings, Settings
        from trading_bot.db.session import get_session, init_db, make_session_factory
        from trading_bot.runtime.orchestrator import _persist_scan_results_to_db

        settings = Settings(
            app=AppSettings(
                state_db_path=str(tmp_path / "state" / "test.db"),
                log_dir=str(tmp_path / "logs"),
                scan_results_path=str(tmp_path / "scan.json"),
            )
        )
        candidate_rows = [
            {
                "ticker": "AAPL",
                "status": "APPROVED",
                "confidence": 0.9,
                "quality": "GREEN",
                "freshness": "fresh",
                "age": "5m",
                "supermodel_score": 0.7,
                "swarm_confidence": 0.8,
                "swarm_sentiment_score": 0.42,
                "details": {
                    "mtf_regime": "strong_uptrend",
                    "consensus": "BUY",
                    "v3_total_score": 8.5,
                    "swarm_sentiment_confidence": 0.72,
                    "mtf_aligned": 3,
                    "entry_volume_ratio": 1.4,
                    "entry_range_ratio": 0.9,
                    "adaptive_rr": 2.5,
                    "v3_strategy": "v3-trend_following",
                },
            }
        ]

        _persist_scan_results_to_db(candidate_rows, settings)

        engine = init_db(settings)
        session = get_session(make_session_factory(engine))
        try:
            features = get_scan_features(session, ticker="AAPL")
            assert len(features) == 1
            assert features[0].swarm_sentiment_score == 0.42
            assert features[0].market_age_minutes == 5
            assert features[0].strategy_tag == "v3-trend_following"
        finally:
            session.close()
            engine.dispose()

    def test_get_scan_features_status_filter(self, session):
        upsert_scan_feature(session, ticker="AAPL", status="APPROVED", action="BUY")
        upsert_scan_feature(session, ticker="MSFT", status="REJECTED", action="HOLD")
        upsert_scan_feature(session, ticker="GOOGL", status="APPROVED", action="HOLD")

        result = get_scan_features(session, status="APPROVED")
        assert len(result) == 2
        assert all(f.status == "APPROVED" for f in result)

    def test_get_scan_features_action_filter(self, session):
        upsert_scan_feature(session, ticker="AAPL", status="APPROVED", action="BUY")
        upsert_scan_feature(session, ticker="MSFT", status="APPROVED", action="HOLD")
        upsert_scan_feature(session, ticker="GOOGL", status="APPROVED", action="BUY")

        result = get_scan_features(session, action="BUY")
        assert len(result) == 2
        assert all(f.action == "BUY" for f in result)

    def test_get_scan_features_regime_filter(self, session):
        upsert_scan_feature(session, ticker="AAPL", status="APPROVED", action="BUY", market_regime="strong_uptrend")
        upsert_scan_feature(session, ticker="MSFT", status="APPROVED", action="BUY", market_regime="strong_downtrend")
        upsert_scan_feature(session, ticker="GOOGL", status="APPROVED", action="BUY", market_regime="strong_uptrend")

        result = get_scan_features(session, market_regime="strong_uptrend")
        assert len(result) == 2
        assert all(f.market_regime == "strong_uptrend" for f in result)

    def test_get_scan_features_quality_filter(self, session):
        upsert_scan_feature(session, ticker="AAPL", status="APPROVED", action="BUY", quality="GREEN")
        upsert_scan_feature(session, ticker="MSFT", status="APPROVED", action="BUY", quality="YELLOW")
        upsert_scan_feature(session, ticker="GOOGL", status="APPROVED", action="BUY", quality="GREEN")

        result = get_scan_features(session, quality="GREEN")
        assert len(result) == 2
        assert all(f.quality == "GREEN" for f in result)

    def test_get_scan_features_strategy_filter(self, session):
        upsert_scan_feature(session, ticker="AAPL", status="APPROVED", action="BUY", strategy_tag="v3-trend_following")
        upsert_scan_feature(session, ticker="MSFT", status="APPROVED", action="BUY", strategy_tag="v3-mean_reversion")
        upsert_scan_feature(session, ticker="GOOGL", status="APPROVED", action="BUY", strategy_tag="v3-trend_following")

        result = get_scan_features(session, strategy_tag="v3-trend_following")
        assert len(result) == 2
        assert all(f.strategy_tag == "v3-trend_following" for f in result)

    def test_get_scan_features_sentiment_bullish(self, session):
        upsert_scan_feature(session, ticker="AAPL", status="APPROVED", action="BUY", swarm_sentiment_score=0.5)
        upsert_scan_feature(session, ticker="MSFT", status="APPROVED", action="BUY", swarm_sentiment_score=-0.4)
        upsert_scan_feature(session, ticker="GOOGL", status="APPROVED", action="BUY", swarm_sentiment_score=0.4)

        result = get_scan_features(session, swarm_sentiment_bucket="bullish")
        assert len(result) == 2
        assert all(f.swarm_sentiment_score is not None and f.swarm_sentiment_score >= 0.35 for f in result)

    def test_get_scan_features_sentiment_bearish(self, session):
        upsert_scan_feature(session, ticker="AAPL", status="APPROVED", action="BUY", swarm_sentiment_score=0.5)
        upsert_scan_feature(session, ticker="MSFT", status="APPROVED", action="BUY", swarm_sentiment_score=-0.4)
        upsert_scan_feature(session, ticker="GOOGL", status="APPROVED", action="BUY", swarm_sentiment_score=-0.5)

        result = get_scan_features(session, swarm_sentiment_bucket="bearish")
        assert len(result) == 2
        assert all(f.swarm_sentiment_score is not None and f.swarm_sentiment_score <= -0.35 for f in result)

    def test_get_scan_features_sentiment_neutral(self, session):
        upsert_scan_feature(session, ticker="AAPL", status="APPROVED", action="BUY", swarm_sentiment_score=0.5)
        upsert_scan_feature(session, ticker="MSFT", status="APPROVED", action="BUY", swarm_sentiment_score=0.1)
        upsert_scan_feature(session, ticker="GOOGL", status="APPROVED", action="BUY", swarm_sentiment_score=-0.1)

        result = get_scan_features(session, swarm_sentiment_bucket="neutral")
        assert len(result) == 2
        assert all(
            f.swarm_sentiment_score is not None and -0.35 < f.swarm_sentiment_score < 0.35
            for f in result
        )

    def test_get_scan_features_combined_filters(self, session):
        upsert_scan_feature(
            session, ticker="AAPL", status="APPROVED", action="BUY",
            market_regime="strong_uptrend", quality="GREEN",
            strategy_tag="v3-trend_following", swarm_sentiment_score=0.5,
        )
        upsert_scan_feature(
            session, ticker="MSFT", status="APPROVED", action="BUY",
            market_regime="strong_uptrend", quality="GREEN",
            strategy_tag="v3-mean_reversion", swarm_sentiment_score=0.5,
        )
        upsert_scan_feature(
            session, ticker="GOOGL", status="APPROVED", action="HOLD",
            market_regime="strong_uptrend", quality="GREEN",
            strategy_tag="v3-trend_following", swarm_sentiment_score=0.5,
        )

        result = get_scan_features(
            session,
            status="APPROVED", action="BUY", market_regime="strong_uptrend",
            quality="GREEN", strategy_tag="v3-trend_following",
        )
        assert len(result) == 1
        assert result[0].ticker == "AAPL"

    def test_get_scan_features_sentiment_unknown(self, session):
        upsert_scan_feature(session, ticker="AAPL", status="APPROVED", action="BUY", swarm_sentiment_score=0.5)
        upsert_scan_feature(session, ticker="MSFT", status="APPROVED", action="BUY")

        result = get_scan_features(session, swarm_sentiment_bucket="unknown")
        assert len(result) == 1
        assert result[0].ticker == "MSFT"


# ──────────────────────────── Positions repository ────────────────────────────


class TestPositionsRepo:
    def test_upsert_position_new(self, session):
        pos = upsert_position(session, ticker="AAPL", quantity=100, average_cost=150.0)
        assert pos.id is not None
        assert pos.ticker == "AAPL"
        assert pos.closed_at is None

    def test_upsert_position_updates_existing(self, session):
        upsert_position(session, ticker="AAPL", quantity=100, average_cost=150.0)
        updated = upsert_position(session, ticker="AAPL", quantity=150, average_cost=155.0)
        assert updated.quantity == 150
        assert updated.average_cost == 155.0

    def test_close_position(self, session):
        upsert_position(session, ticker="AAPL", quantity=100, average_cost=150.0)
        closed = close_position(session, "AAPL")
        assert closed is not None
        assert closed.closed_at is not None

    def test_close_position_not_found(self, session):
        result = close_position(session, "NOPE")
        assert result is None

    def test_get_open_positions(self, session):
        upsert_position(session, ticker="AAPL", quantity=100, average_cost=150.0)
        upsert_position(session, ticker="GOOGL", quantity=50, average_cost=90.0)
        open_positions = get_open_positions(session)
        assert len(open_positions) == 2

    def test_get_open_positions_excludes_closed(self, session):
        upsert_position(session, ticker="AAPL", quantity=100, average_cost=150.0)
        close_position(session, "AAPL")
        open_positions = get_open_positions(session)
        assert len(open_positions) == 0

    def test_upsert_position_with_optional_fields(self, session):
        pos = upsert_position(
            session, ticker="AAPL", quantity=100, average_cost=150.0,
            stop_loss=140.0, profit_target=170.0, highest_high=160.0,
            strategy_tag="breakout",
        )
        assert pos.stop_loss == 140.0
        assert pos.profit_target == 170.0
        assert pos.highest_high == 160.0
        assert pos.strategy_tag == "breakout"


# ──────────────────────────── Scan results repository ────────────────────────────


class TestScanResultsRepo:
    def test_upsert_scan_result(self, session):
        result = upsert_scan_result(
            session, ticker="AAPL", action="BUY", confidence=0.85,
            score=0.9, strategy_tag="momentum",
        )
        assert result.id is not None
        assert result.ticker == "AAPL"
        assert result.action == "BUY"

    def test_upsert_with_reasons_and_details(self, session):
        result = upsert_scan_result(
            session, ticker="AAPL", action="BUY", confidence=0.85,
            reasons=["strong momentum", "volume surge"],
            details={"v3_score": 0.8},
        )
        assert result.reasons is not None
        assert result.details is not None

    def test_get_scan_results_by_ticker(self, session):
        upsert_scan_result(session, ticker="AAPL", action="BUY", confidence=0.85)
        upsert_scan_result(session, ticker="GOOGL", action="HOLD", confidence=0.5)
        results = get_scan_results(session, ticker="AAPL")
        assert len(results) == 1
        assert results[0].ticker == "AAPL"

    def test_get_scan_results_by_action(self, session):
        upsert_scan_result(session, ticker="AAPL", action="BUY", confidence=0.85)
        upsert_scan_result(session, ticker="GOOGL", action="HOLD", confidence=0.5)
        results = get_scan_results(session, action="BUY")
        assert len(results) == 1
        assert results[0].action == "BUY"

    def test_get_scan_results_with_limit(self, session):
        for i in range(5):
            upsert_scan_result(session, ticker="AAPL", action="BUY", confidence=0.8 + i * 0.01)
        results = get_scan_results(session, limit=3)
        assert len(results) == 3


# ──────────────────────────── Portfolio snapshots repository ────────────────────────────


class TestPortfolioSnapshotsRepo:
    def test_create_snapshot(self, session):
        snap = create_snapshot(session, cash=5000.0, equity=10000.0)
        assert snap.id is not None
        assert snap.cash == 5000.0
        assert snap.equity == 10000.0

    def test_create_snapshot_with_optional_fields(self, session):
        snap = create_snapshot(
            session, cash=5000.0, equity=10000.0,
            unrealized_pnl=500.0, realized_pnl=200.0, num_positions=3,
        )
        assert snap.unrealized_pnl == 500.0
        assert snap.realized_pnl == 200.0
        assert snap.num_positions == 3

    def test_get_snapshots(self, session):
        create_snapshot(session, cash=5000.0, equity=10000.0)
        create_snapshot(session, cash=6000.0, equity=11000.0)
        snapshots = get_snapshots(session)
        assert len(snapshots) == 2

    def test_get_snapshots_with_limit(self, session):
        for i in range(5):
            create_snapshot(session, cash=5000.0 + i, equity=10000.0 + i)
        snapshots = get_snapshots(session, limit=3)
        assert len(snapshots) == 3

    def test_get_snapshots_since(self, session):
        create_snapshot(session, cash=5000.0, equity=10000.0)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=1)
        create_snapshot(session, cash=6000.0, equity=11000.0)
        snapshots = get_snapshots(session, since=cutoff)
        assert len(snapshots) >= 1


# ──────────────────────────── Model predictions repository ────────────────────────────


class TestModelPredictionsRepo:
    def test_upsert_prediction(self, session):
        pred = upsert_prediction(
            session, ticker="AAPL", action=1, confidence=0.8,
            model_path="models/rl_agent.zip",
        )
        assert pred.id is not None
        assert pred.ticker == "AAPL"
        assert pred.action == 1
        assert pred.confidence == 0.8

    def test_upsert_prediction_with_observation_hash(self, session):
        pred = upsert_prediction(
            session, ticker="AAPL", action=1, confidence=0.8,
            observation=[0.1, 0.2, 0.3, 0.4],
        )
        assert pred.observation_hash is not None
        assert len(pred.observation_hash) == 64  # SHA-256 hex digest

    def test_upsert_prediction_without_observation(self, session):
        pred = upsert_prediction(session, ticker="AAPL", action=0, confidence=0.5)
        assert pred.observation_hash is None

    def test_get_predictions_by_ticker(self, session):
        upsert_prediction(session, ticker="AAPL", action=1, confidence=0.8)
        upsert_prediction(session, ticker="GOOGL", action=2, confidence=0.6)
        results = get_predictions(session, ticker="AAPL")
        assert len(results) == 1
        assert results[0].ticker == "AAPL"

    def test_get_predictions_with_limit(self, session):
        for i in range(5):
            upsert_prediction(session, ticker="AAPL", action=1, confidence=0.8)
        results = get_predictions(session, limit=3)
        assert len(results) == 3


# ──────────────────────────── Events repository ────────────────────────────


class TestEventsRepo:
    def test_log_event_basic(self, session):
        event = log_event(session, event_type="ORDER_FILL", entity_type="trade", entity_id=1)
        assert event.id is not None
        assert event.event_type == "ORDER_FILL"

    def test_log_event_with_dict_details(self, session):
        event = log_event(
            session, event_type="SIGNAL", details={"ticker": "AAPL", "action": "BUY"},
        )
        assert event.details is not None
        assert "AAPL" in event.details

    def test_log_event_with_string_details(self, session):
        event = log_event(session, event_type="SIGNAL", details="plain string")
        assert event.details == "plain string"

    def test_get_events_all(self, session):
        log_event(session, event_type="A")
        log_event(session, event_type="B")
        events = get_events(session)
        assert len(events) == 2

    def test_get_events_by_type(self, session):
        log_event(session, event_type="SIGNAL")
        log_event(session, event_type="ORDER")
        events = get_events(session, event_type="SIGNAL")
        assert len(events) == 1
        assert events[0].event_type == "SIGNAL"

    def test_get_events_with_limit(self, session):
        for i in range(5):
            log_event(session, event_type="TICK")
        events = get_events(session, limit=3)
        assert len(events) == 3

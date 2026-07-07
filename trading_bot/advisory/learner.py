from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from trading_bot.advisory.models import AdvisoryObservation, AdvisoryRecommendation, AdvisoryRunSummary
from trading_bot.advisory.reporting import format_daily_report_markdown
from trading_bot.config.settings import ScoutSettings, Settings
from trading_bot.data import market_data
from trading_bot.db.models import ScanFeature, Trade
from trading_bot.db.session import get_session, init_db, make_session_factory
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.reports.exporters import export_json
from trading_bot.runtime.snapshots import write_snapshot
from trading_bot.scout import build_scout_candidates


@dataclass(frozen=True)
class AdvisoryPaths:
    root: Path
    observations: Path
    state: Path
    main: Path
    cheap: Path
    latest_report: Path
    scout_override: Path
    daily_report: Path


def advisory_paths(settings: Settings) -> AdvisoryPaths:
    root = Path(settings.app.advisory_dir)
    return AdvisoryPaths(
        root=root,
        observations=root / "observations.jsonl",
        state=root / "learner_state.json",
        main=root / "recommendations.main_midcap.json",
        cheap=root / "recommendations.cheap_stocks.json",
        latest_report=root / "latest_report.json",
        scout_override=root / "scout_override.yaml",
        daily_report=root / "Daily report.md",
    )


def load_latest_advisory_report(settings: Settings) -> dict[str, Any]:
    path = advisory_paths(settings).latest_report
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_scout_override(settings: Settings) -> dict[str, Any]:
    path = advisory_paths(settings).scout_override
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def apply_scout_override(symbols: list[str], settings: Settings, limit: int | None = None) -> list[str]:
    override = load_scout_override(settings)
    main = override.get("main_midcap") if isinstance(override, dict) else None
    promote = main.get("promote_symbols", []) if isinstance(main, dict) else []
    avoid = {str(value).upper().strip() for value in (main.get("avoid_symbols", []) if isinstance(main, dict) else []) if str(value).strip()}
    merged: list[str] = []
    seen: set[str] = set()
    for raw_symbol in [*promote, *symbols]:
        symbol = str(raw_symbol).upper().strip()
        if not symbol or symbol in seen or symbol in avoid:
            continue
        seen.add(symbol)
        merged.append(symbol)
        if limit is not None and len(merged) >= limit:
            break
    return merged


def run_advisory_learner(settings: Settings, write_daily_report: bool = False) -> AdvisoryRunSummary:
    if not settings.advisory.enabled:
        return AdvisoryRunSummary()

    paths = advisory_paths(settings)
    paths.root.mkdir(parents=True, exist_ok=True)
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"
    offset = _load_offset(paths.state)
    new_events, new_offset = _read_new_events(log_path, offset)
    observations = [_event_to_observation(event) for event in new_events]
    observations = [row for row in observations if row is not None]
    _append_observations(paths.observations, observations)
    _save_offset(paths.state, new_offset)

    all_observations = _read_observations(paths.observations)
    orders = PortfolioLedger(Path(settings.app.state_db_path)).list_order_rows()
    source_names_by_symbol = _source_names_by_symbol(Path(settings.app.universe_candidates_path))
    analytics = _analytics_metrics_by_symbol(settings)
    main_rows = _build_main_recommendations(all_observations, orders, settings, source_names_by_symbol, analytics)
    cheap_rows = _build_cheap_recommendations(settings, main_rows, source_names_by_symbol)
    report = {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "summary": {
            "observations": len(all_observations),
            "main_recommendations": len(main_rows),
            "cheap_recommendations": len(cheap_rows),
            "promoted_symbols": len([row for row in main_rows if row["approval_rate"] >= settings.advisory.min_hit_rate_for_promote]),
            "avoided_symbols": len([row for row in main_rows if row["approval_rate"] == 0.0]),
        },
        "main_midcap": main_rows,
        "cheap_stocks": cheap_rows,
    }
    write_snapshot(paths.main, {"mode": "advisory_main", "recommendations": main_rows})
    write_snapshot(paths.cheap, {"mode": "advisory_cheap", "recommendations": cheap_rows})
    export_json(report, paths.latest_report)

    promote_symbols = [
        row["ticker"]
        for row in main_rows
        if float(row.get("approval_rate", 0.0)) >= settings.advisory.min_hit_rate_for_promote
    ][: settings.advisory.main_limit]
    avoid_symbols = [
        row["ticker"]
        for row in main_rows
        if row.get("observations", 0) >= settings.advisory.min_observations_per_symbol and float(row.get("approval_rate", 0.0)) == 0.0
    ]
    override = {
        "generated_at": report["generated_at"],
        "mode": "advisory_only",
        "main_midcap": {
            "promote_symbols": promote_symbols,
            "avoid_symbols": avoid_symbols,
        },
        "cheap_stocks": {
            "separate_watchlist": [row["ticker"] for row in cheap_rows[: settings.advisory.cheap_limit]],
        },
        "notes": [
            "Cheap-stock ideas are intentionally excluded from main scout overrides.",
        ],
    }
    paths.scout_override.write_text(yaml.safe_dump(override, sort_keys=False), encoding="utf-8")

    if write_daily_report:
        paths.daily_report.write_text(format_daily_report_markdown(report), encoding="utf-8")

    return AdvisoryRunSummary(
        observations_added=len(observations),
        main_recommendations=len(main_rows),
        cheap_recommendations=len(cheap_rows),
        promoted_symbols=promote_symbols,
        avoided_symbols=avoid_symbols,
    )


def _load_offset(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return int(payload.get("offset", 0))
    except (TypeError, ValueError):
        return 0


def _save_offset(path: Path, offset: int) -> None:
    export_json({"offset": offset}, path)


def _read_new_events(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    if offset > size:
        offset = 0
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        payload = handle.read()
        new_offset = handle.tell()
    events: list[dict[str, Any]] = []
    for raw_line in payload.splitlines():
        if not raw_line.strip():
            continue
        try:
            loaded = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            events.append(loaded)
    return events, new_offset


def _event_to_observation(event: dict[str, Any]) -> AdvisoryObservation | None:
    if event.get("command") != "scan":
        return None
    ticker = str(event.get("ticker", "") or "").upper().strip()
    if not ticker:
        return None
    timestamp = (
        str(event.get("timestamp") or event.get("generated_at") or datetime.now(UTC).replace(microsecond=0).isoformat())
    )
    return AdvisoryObservation(
        ticker=ticker,
        status=str(event.get("status", "") or ""),
        reason=str(event.get("reason", "") or ""),
        confidence=_safe_float(event.get("confidence")),
        quality=str(event.get("quality", "") or ""),
        entry_price=_optional_float(event.get("entry_price", event.get("entry"))),
        supermodel_decision=str(event.get("supermodel_decision", "") or ""),
        consensus=str(event.get("consensus", "") or ""),
        observed_at=timestamp,
    )


def _append_observations(path: Path, rows: list[AdvisoryObservation]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row.model_dump_json() + "\n")


def _read_observations(path: Path) -> list[AdvisoryObservation]:
    if not path.exists():
        return []
    rows: list[AdvisoryObservation] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            rows.append(AdvisoryObservation.model_validate_json(raw_line))
        except Exception:
            continue
    return rows


def _build_main_recommendations(
    observations: list[AdvisoryObservation],
    orders: list[dict[str, object]],
    settings: Settings,
    source_names_by_symbol: dict[str, list[str]],
    analytics: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    trades = _trade_metrics_by_symbol(orders)
    grouped: dict[str, list[AdvisoryObservation]] = defaultdict(list)
    for row in observations:
        grouped[row.ticker].append(row)

    recommendations: list[dict[str, Any]] = []
    for ticker in sorted(set(grouped) | set(analytics)):
        rows = grouped.get(ticker, [])
        analytic = analytics.get(ticker, {})
        observation_count = len(rows) or int(analytic.get("observations", 0.0))
        if observation_count < settings.advisory.min_observations_per_symbol:
            continue
        counts = Counter(row.status for row in rows)
        approved = counts.get("APPROVED", 0)
        rejected = counts.get("REJECTED", 0)
        total = len(rows)
        approval_rate = _safe_div(float(approved), float(total)) if total else float(analytic.get("approval_rate", 0.0))
        reject_rate = _safe_div(float(rejected), float(total)) if total else float(analytic.get("reject_rate", 0.0))
        avg_confidence = (sum(row.confidence for row in rows) / total) if total else float(analytic.get("avg_confidence", 0.0))
        trade = trades.get(ticker, {})
        closed = float(trade.get("closed", 0.0)) or float(analytic.get("closed_trades", 0.0))
        wins = float(trade.get("wins", 0.0)) or float(analytic.get("winning_trades", 0.0))
        win_rate = _safe_div(wins, closed)
        net_pnl = float(trade.get("net_pnl", 0.0)) or float(analytic.get("net_pnl", 0.0))
        supermodel_score = float(analytic.get("avg_supermodel_score", 0.0))
        v3_total_score = float(analytic.get("avg_v3_total_score", 0.0))
        score = round(
            (approval_rate * 0.35)
            + (avg_confidence * 0.20)
            + (win_rate * 0.20)
            + _pnl_component(net_pnl)
            + (supermodel_score * 0.15)
            + _v3_component(v3_total_score)
            - (reject_rate * 0.15),
            4,
        )
        last_price = next((row.entry_price for row in reversed(rows) if row.entry_price is not None), None)
        if last_price is None:
            last_price = _optional_float(trade.get("last_entry_price", analytic.get("last_entry_price")))
        reasons = [reason for reason, _ in Counter(row.reason for row in rows if row.reason).most_common(3)]
        recommendation = AdvisoryRecommendation(
            ticker=ticker,
            score=score,
            bucket="cheap" if last_price is not None and last_price <= settings.advisory.cheap_stock_max_price else "main_midcap",
            observations=observation_count,
            approval_rate=round(approval_rate, 4),
            win_rate=round(win_rate, 4) if closed else None,
            net_pnl=round(net_pnl, 2),
            reasons=reasons,
            source_names=source_names_by_symbol.get(ticker, []),
        )
        recommendations.append(recommendation.model_dump())

    recommendations.sort(key=lambda row: (-float(row["score"]), row["ticker"]))
    main_rows = [row for row in recommendations if row["bucket"] == "main_midcap"]
    return main_rows[: settings.advisory.main_limit]


def _build_cheap_recommendations(
    settings: Settings,
    main_rows: list[dict[str, Any]],
    source_names_by_symbol: dict[str, list[str]],
) -> list[dict[str, Any]]:
    favorable_sources: set[str] = set()
    for row in main_rows:
        for source_name in source_names_by_symbol.get(str(row["ticker"]), []):
            favorable_sources.add(source_name)
    cheap_settings = settings.scout.model_copy(
        update={
            "min_price": 0.01,
            "min_avg_dollar_volume": min(settings.scout.min_avg_dollar_volume, 1_000_000.0),
            "max_universe_size": max(settings.advisory.cheap_limit, settings.scout.max_universe_size),
            "max_snapshot_candidates": max(settings.advisory.cheap_limit * 5, settings.scout.max_snapshot_candidates),
        }
    )
    rows = market_data.fetch_small_cap_candidates(
        limit=max(settings.advisory.cheap_limit * 5, settings.scout.max_snapshot_candidates),
        screeners=settings.scout.screeners,
    )
    scout_result = build_scout_candidates(rows, cheap_settings)
    recommendations: list[dict[str, Any]] = []
    for candidate in scout_result.candidates:
        if not candidate.included:
            continue
        if candidate.price is None or candidate.price > settings.advisory.cheap_stock_max_price:
            continue
        overlap = len(favorable_sources.intersection(set(candidate.source_names)))
        recommendation = AdvisoryRecommendation(
            ticker=candidate.ticker,
            score=round((candidate.scout_score / 100.0) + (overlap * 0.10), 4),
            bucket="cheap",
            observations=0,
            approval_rate=0.0,
            win_rate=None,
            net_pnl=0.0,
            reasons=candidate.reasons,
            source_names=candidate.source_names,
        )
        recommendations.append(recommendation.model_dump())
    recommendations.sort(key=lambda row: (-float(row["score"]), row["ticker"]))
    return recommendations[: settings.advisory.cheap_limit]


def _trade_metrics_by_symbol(orders: list[dict[str, object]]) -> dict[str, dict[str, float]]:
    metrics: dict[str, dict[str, float]] = defaultdict(lambda: {"closed": 0.0, "wins": 0.0, "net_pnl": 0.0, "last_entry_price": 0.0})
    for row in orders:
        ticker = str(row.get("ticker", "") or "").upper().strip()
        if not ticker:
            continue
        if str(row.get("side", "")) == "BUY":
            metrics[ticker]["last_entry_price"] = _safe_float(row.get("fill_price"))
            continue
        if str(row.get("side", "")) != "SELL":
            continue
        pnl = _safe_float(row.get("pnl"))
        metrics[ticker]["closed"] += 1.0
        metrics[ticker]["net_pnl"] += pnl
        if pnl > 0:
            metrics[ticker]["wins"] += 1.0
    return metrics


def _source_names_by_symbol(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("candidates")
    if not isinstance(rows, list):
        return {}
    mapping: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker", "") or "").upper().strip()
        if not ticker:
            continue
        source_names = row.get("source_names")
        if isinstance(source_names, list):
            mapping[ticker] = [str(value) for value in source_names if str(value).strip()]
    return mapping


def _analytics_metrics_by_symbol(settings: Settings) -> dict[str, dict[str, float]]:
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        features = session.execute(select(ScanFeature)).scalars().all()
        trades = session.execute(select(Trade)).scalars().all()
    finally:
        session.close()
        engine.dispose()

    metrics: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "observations": 0.0,
            "approved": 0.0,
            "rejected": 0.0,
            "confidence_sum": 0.0,
            "supermodel_score_sum": 0.0,
            "v3_total_score_sum": 0.0,
            "closed_trades": 0.0,
            "winning_trades": 0.0,
            "net_pnl": 0.0,
            "last_entry_price": 0.0,
        }
    )
    for feature in features:
        row = metrics[feature.ticker.upper()]
        row["observations"] += 1.0
        if feature.status == "APPROVED":
            row["approved"] += 1.0
        elif feature.status == "REJECTED":
            row["rejected"] += 1.0
        row["confidence_sum"] += _safe_float(feature.confidence)
        row["supermodel_score_sum"] += _safe_float(feature.supermodel_score)
        row["v3_total_score_sum"] += _safe_float(feature.v3_total_score)
    for trade in trades:
        ticker = trade.ticker.upper()
        row = metrics[ticker]
        row["last_entry_price"] = _safe_float(trade.entry_price) or row["last_entry_price"]
        if trade.pnl is None:
            continue
        row["closed_trades"] += 1.0
        row["net_pnl"] += _safe_float(trade.pnl)
        if _safe_float(trade.pnl) > 0:
            row["winning_trades"] += 1.0

    normalized: dict[str, dict[str, float]] = {}
    for ticker, row in metrics.items():
        observations = row["observations"]
        normalized[ticker] = {
            "observations": observations,
            "approval_rate": _safe_div(row["approved"], observations),
            "reject_rate": _safe_div(row["rejected"], observations),
            "avg_confidence": _safe_div(row["confidence_sum"], observations),
            "avg_supermodel_score": _safe_div(row["supermodel_score_sum"], observations),
            "avg_v3_total_score": _safe_div(row["v3_total_score_sum"], observations),
            "closed_trades": row["closed_trades"],
            "winning_trades": row["winning_trades"],
            "net_pnl": row["net_pnl"],
            "last_entry_price": row["last_entry_price"],
        }
    return normalized


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_div(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _pnl_component(value: float) -> float:
    if value <= 0:
        return max(-0.20, value / 1000.0)
    return min(0.20, value / 1000.0)


def _v3_component(value: float) -> float:
    if value <= 0:
        return 0.0
    return min(0.10, value / 100.0)

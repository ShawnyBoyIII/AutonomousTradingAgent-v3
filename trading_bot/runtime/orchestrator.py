from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from trading_bot.config.settings import Settings
from trading_bot.data import market_data
from trading_bot.data.indicators import add_atr, add_bollinger_bands, add_ema, add_rsi, add_sma, add_vwap
from trading_bot.execution.fills import apply_slippage
from trading_bot.execution.order_manager import submit_signal_as_order
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.order import OrderRequest
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.models.signal import TradeSignal
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.portfolio.performance import compute_portfolio_heat, compute_unrealized_pnl
from trading_bot.risk.correlation import compute_portfolio_correlation
from trading_bot.runtime.decision_log import append_decision_event
from trading_bot.runtime.snapshots import write_snapshot
from trading_bot.risk.risk_manager import evaluate_signal
from trading_bot.strategy.intraday_signal_engine import generate_recent_signal_with_reason
from trading_bot.strategy.signal_quality import (
    adapt_signal_to_volatility_regime,
    evaluate_signal_quality,
)
from trading_bot.strategy.supermodel import build_stacked_signal
from trading_bot.rl.utils import rl_model_meta_path, rl_model_symbols

logger = logging.getLogger(__name__)

_sector_cache: dict[str, str] = {}


def _get_sector(ticker: str) -> str:
    """Return locally known sector ETF for a ticker.

    Runtime trading paths must not make metadata network calls. Unknown sectors
    are left unblocked instead of guessing.
    """
    if ticker in _sector_cache:
        return _sector_cache[ticker]
    from trading_bot.strategy.dynamic_watchlist import _SECTOR_MAP

    sector = _SECTOR_MAP.get(ticker.upper().strip(), "")
    _sector_cache[ticker] = sector
    return sector


def _sector_concentration_exceeded(ticker: str, state, settings, pending_value: float = 0.0) -> str | None:
    max_pct = settings.risk.max_sector_concentration_pct
    if max_pct <= 0 or max_pct >= 1.0:
        return None
    if not state.positions:
        return None
    target_sector = _get_sector(ticker)
    if not target_sector:
        return None
    sector_value = 0.0
    total_value = state.equity
    if total_value <= 0:
        return None
    for t, pos in state.positions.items():
        if _get_sector(t) == target_sector:
            sector_value += pos.quantity * pos.average_cost
    projected_pct = (sector_value + max(0.0, pending_value)) / total_value
    if projected_pct > max_pct:
        return f"sector={target_sector} projected {projected_pct:.0%} > {max_pct:.0%}"
    return None


def _extract_close_history(frame: pd.DataFrame) -> list[float]:
    for column in ("close", "Close"):
        if column in frame.columns:
            values = []
            for raw_value in frame[column].tolist():
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value > 0:
                    values.append(value)
            return values
    return []


def _correlation_context_for_candidate(
    ticker: str,
    state: PortfolioState,
    settings: Settings,
    history_cache: dict[str, list[float]],
) -> tuple[float | None, float | None]:
    tracked_tickers = sorted({ticker, *(symbol for symbol, pos in state.positions.items() if pos.quantity > 0)})
    if len(tracked_tickers) < 2:
        return None, None

    price_history: dict[str, list[float]] = {}
    for symbol in tracked_tickers:
        history = history_cache.get(symbol)
        if history is None:
            try:
                frame = market_data.fetch_bars(
                    symbol,
                    period="3mo",
                    interval="1d",
                    settings=settings.market_data,
                )
            except Exception as exc:
                logger.debug("correlation history unavailable symbol=%s error=%s", symbol, exc)
                history = []
            else:
                history = _extract_close_history(frame)
            history_cache[symbol] = history
        if history:
            price_history[symbol] = history

    candidate_positions = dict(state.positions)
    if ticker not in candidate_positions:
        candidate_positions[ticker] = Position(
            ticker=ticker,
            quantity=1,
            average_cost=1.0,
        )

    result = compute_portfolio_correlation(
        candidate_positions,
        price_history,
        max_avg_correlation=settings.monitoring.max_avg_correlation,
    )
    if result.pair_count == 0:
        return None, settings.monitoring.max_avg_correlation
    return result.avg_correlation, settings.monitoring.max_avg_correlation


def _recently_exited(ticker: str, state, cooldown_minutes: int) -> bool:
    ts = state.last_exited_at.get(ticker)
    if not ts:
        return False
    try:
        exited_at = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    now = datetime.now(timezone.utc) if exited_at.tzinfo else datetime.now()
    return (now - exited_at).total_seconds() < cooldown_minutes * 60


def run_scan(
    symbols: list[str],
    settings: Settings,
    include_details: bool = False,
) -> dict[str, object]:
    ledger = PortfolioLedger(Path(settings.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    approved_results: list[tuple[dict[str, object], str]] = []
    other_results: list[str] = []
    candidate_rows: list[dict[str, object]] = []
    open_tickers = set(state.positions)
    correlation_history_cache: dict[str, list[float]] = {}
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"
    rl_action_counts = {"hold": 0, "buy": 0, "sell": 0}
    rl_confidence_total = 0.0
    rl_confidence_count = 0
    rl_unsupported = 0

    # V2.5: Calculate portfolio heat before scanning
    portfolio_heat = _calculate_portfolio_heat(state, settings)

    # V2.5: Check kill switch before trading
    from trading_bot.safety.kill_switch import check_kill_switch_before_trade
    allowed, reason = check_kill_switch_before_trade(ledger)
    if not allowed:
        return {
            "lines": [f"KILL_SWITCH: {reason}"],
            "summary": {
                "symbols": len([value for value in symbols if value.strip()]),
                "approved": 0,
                "green": 0,
                "yellow": 0,
                "rejected": 0,
                "no_signal": 0,
                "errors": 0,
            },
            "candidates": [],
        }

    # V3.1: Circuit breaker — auto-halt on consecutive losses / max drawdown
    from trading_bot.safety.circuit_breaker import check_circuit_breakers
    cb_allowed, cb_reason = check_circuit_breakers(ledger, settings)
    if not cb_allowed:
        append_decision_event(
            log_path,
            {"command": "scan", "status": "CIRCUIT_BREAKER", "reason": cb_reason},
        )
        return {
            "lines": [f"CIRCUIT_BREAKER: {cb_reason}"],
            "summary": {
                "symbols": len([value for value in symbols if value.strip()]),
                "approved": 0,
                "green": 0,
                "yellow": 0,
                "rejected": 0,
                "no_signal": 0,
                "errors": 0,
            },
            "candidates": [],
        }


    for symbol in (value.strip() for value in symbols if value.strip()):
        try:
            signal, no_signal_reason, details = _build_signal_result(symbol, settings)
            rl_action = details.get("rl_action")
            if rl_action in (0, 1, 2):
                rl_action_counts[{0: "hold", 1: "buy", 2: "sell"}[int(rl_action)]] += 1
                confidence = _finite_float(details.get("rl_confidence"))
                if confidence is not None:
                    rl_confidence_total += confidence
                    rl_confidence_count += 1
            elif "rl_trained_symbols" in details:
                rl_unsupported += 1
            counter_result = _evaluate_counter_thesis_for_signal(symbol, signal, settings)
            if counter_result is not None:
                _augment_details_with_counter_thesis(details, counter_result)
            if signal is None:
                details.update(
                    build_stacked_signal(
                        symbol,
                        signal,
                        details,
                        settings=settings.supermodel,
                    ).to_details()
                )
                detail_text = _format_scan_details(details) if include_details else ""
                append_decision_event(
                    log_path,
                    {
                        "command": "scan",
                        "ticker": symbol,
                        "status": "NO_SIGNAL",
                        "reason": no_signal_reason,
                        **_paper_evidence_fields(details),
                    },
                )
                other_results.append(f"{symbol} NO_SIGNAL reason={no_signal_reason}{detail_text}")
                row = {"ticker": symbol, "status": "NO_SIGNAL", "reason": no_signal_reason}
                _attach_supermodel_row_fields(row, details)
                if include_details:
                    row["details"] = details
                candidate_rows.append(row)
                continue

            stacked = build_stacked_signal(symbol, signal, details, settings=settings.supermodel)
            details.update(stacked.to_details())
            detail_text = _format_scan_details(details) if include_details else ""
            if stacked.decision == "block":
                append_decision_event(
                    log_path,
                    {
                        "command": "scan",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": f"supermodel block (score={stacked.score:.2f})",
                        **_paper_evidence_fields(details),
                    },
                )
                other_results.append(
                    f"{symbol} REJECTED supermodel block score={stacked.score:.2f}{detail_text}",
                )
                row = {
                    "ticker": symbol,
                    "status": "REJECTED",
                    "reason": f"supermodel block (score={stacked.score:.2f})",
                }
                _attach_supermodel_row_fields(row, details)
                if include_details:
                    row["details"] = details
                candidate_rows.append(row)
                continue

            # V2.5: Fetch ATR for volatility-adjusted sizing
            atr = _fetch_atr(symbol, settings) if settings.risk.use_atr_sizing else None
            avg_correlation, max_avg_correlation = _correlation_context_for_candidate(
                symbol,
                state,
                settings,
                correlation_history_cache,
            )

            decision = evaluate_signal(
                signal=signal,
                account_equity=state.equity,
                open_tickers=open_tickers,
                portfolio_heat_pct=portfolio_heat,
                atr=atr,
                risk_settings=settings.risk,
                counter_thesis=counter_result,
                avg_correlation=avg_correlation,
                max_avg_correlation=max_avg_correlation,
            )
            if not decision.approved:
                append_decision_event(
                    log_path,
                    {
                        "command": "scan",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": decision.reason,
                        **_paper_evidence_fields(details),
                    },
                )
                other_results.append(f"{symbol} REJECTED {decision.reason}{detail_text}")
                row = {
                    "ticker": symbol,
                    "status": "REJECTED",
                    "reason": decision.reason,
                }
                _attach_supermodel_row_fields(row, details)
                if include_details:
                    row["details"] = details
                candidate_rows.append(row)
                continue

            open_tickers.add(symbol)
            market_age = _market_data_age(signal.timestamp)
            market_status = _market_data_status(
                signal.timestamp,
                settings.market_data.intraday_interval,
                max_age_minutes=settings.market_data.max_data_age_minutes,
            )
            quality = _scan_quality(details)
            append_decision_event(
                log_path,
                {
                    "command": "scan",
                    "ticker": symbol,
                    "status": "APPROVED",
                    "confidence": signal.confidence,
                    "position_size": decision.position_size,
                    "entry_price": signal.entry_price,
                    **(
                        {"counter_thesis": counter_result.to_dict()}
                        if counter_result is not None and counter_result.findings
                        else {}
                    ),
                    **_paper_evidence_fields(details),
                },
            )
            row = {
                "ticker": symbol,
                "status": "APPROVED",
                "quality": quality,
                "freshness": market_status,
                "age": market_age,
                "timestamp": signal.timestamp.isoformat(),
                "last": round(signal.entry_price, 2),
                "qty": decision.position_size,
                "rr": round(signal.risk_reward_ratio, 2),
                "confidence": round(signal.confidence, 2),
                "risk": round(decision.dollar_risk, 2),
                "allocation": round(
                    (signal.entry_price * decision.position_size) / state.equity,
                    2,
                ),
                "entry": round(signal.entry_price, 2),
                "stop": round(signal.stop_loss, 2),
                "target": round(signal.profit_target, 2),
                "reasons": signal.reasons,
            }
            _attach_supermodel_row_fields(row, details)
            if include_details:
                row["details"] = details
            candidate_rows.append(row)
            approved_results.append(
                (
                    row,
                    f"{symbol} APPROVED "
                    f"quality={quality} "
                    f"status={market_status} "
                    f"age={market_age} "
                    f"ts={signal.timestamp.isoformat()} "
                    f"last={signal.entry_price:.2f} "
                    f"qty={decision.position_size} "
                    f"rr={signal.risk_reward_ratio:.2f} "
                    f"conf={signal.confidence:.2f} "
                    f"risk=${decision.dollar_risk:.2f} "
                    f"alloc={(signal.entry_price * decision.position_size) / state.equity:.2f} "
                    f"entry={signal.entry_price:.2f} "
                    f"stop={signal.stop_loss:.2f} "
                    f"target={signal.profit_target:.2f} "
                    f"reasons={'; '.join(signal.reasons)}"
                    f"{detail_text}",
                )
            )
        except Exception as exc:
            error_evidence: dict[str, object] = {}
            append_decision_event(
                log_path,
                {
                    "command": "scan",
                    "ticker": symbol,
                    "status": "ERROR",
                    "error": str(exc),
                    **error_evidence,
                },
            )
            other_results.append(f"{symbol} ERROR {exc}")
            row = {"ticker": symbol, "status": "ERROR", "error": str(exc)}
            row.update(error_evidence)
            candidate_rows.append(row)

    approved_results.sort(key=lambda item: _scan_row_sort_key(item[0]), reverse=True)
    candidate_rows.sort(key=_scan_row_sort_key, reverse=True)
    lines = [value for _, value in approved_results] + other_results
    summary = {
        "symbols": len([value for value in symbols if value.strip()]),
        "approved": sum(1 for row in candidate_rows if row["status"] == "APPROVED"),
        "green": sum(1 for row in candidate_rows if row.get("quality") == "GREEN"),
        "yellow": sum(1 for row in candidate_rows if row.get("quality") == "YELLOW"),
        "rejected": sum(1 for row in candidate_rows if row["status"] == "REJECTED"),
        "no_signal": sum(1 for row in candidate_rows if row["status"] == "NO_SIGNAL"),
        "errors": sum(1 for row in candidate_rows if row["status"] == "ERROR"),
    }
    if getattr(settings, "rl", None) is not None and settings.rl.enabled:
        summary.update(
            {
                "rl_buy": rl_action_counts["buy"],
                "rl_hold": rl_action_counts["hold"],
                "rl_sell": rl_action_counts["sell"],
                "rl_unsupported": rl_unsupported,
                "rl_avg_confidence": round(
                    rl_confidence_total / rl_confidence_count,
                    2,
                )
                if rl_confidence_count
                else 0.0,
            }
        )
    supermodel_decisions = [row["supermodel_decision"] for row in candidate_rows if row.get("supermodel_decision")]
    if supermodel_decisions:
        summary.update(
            {
                "supermodel_support": sum(1 for value in supermodel_decisions if value == "support"),
                "supermodel_caution": sum(1 for value in supermodel_decisions if value == "caution"),
                "supermodel_block": sum(1 for value in supermodel_decisions if value == "block"),
                "supermodel_no_signal": sum(1 for value in supermodel_decisions if value == "no_signal"),
            }
        )
    write_snapshot(
        settings.app.scan_results_path,
        {
            "mode": "scan",
            "summary": summary,
            "candidates": candidate_rows,
        },
    )
    _persist_scan_results_to_db(candidate_rows, settings)
    return {"lines": lines, "summary": summary, "candidates": candidate_rows}


def _attach_supermodel_row_fields(row: dict[str, object], details: dict[str, object]) -> None:
    if details.get("supermodel_decision"):
        row["supermodel_decision"] = details["supermodel_decision"]
        row["supermodel_score"] = details.get("supermodel_score")








def run_paper_trade(symbols: list[str], settings: Settings, dry_run: bool = False) -> list[str]:
    ledger = PortfolioLedger(Path(settings.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    broker = PaperBroker(
        starting_cash=state.cash,
        fee_per_order=settings.paper.fee_per_order,
        slippage_bps=settings.paper.slippage_bps,
        dynamic_slippage_enabled=settings.paper.dynamic_slippage_enabled,
        dynamic_slippage_notional_bps_per_10k=settings.paper.dynamic_slippage_notional_bps_per_10k,
        dynamic_slippage_low_price_boost_bps=settings.paper.dynamic_slippage_low_price_boost_bps,
        dynamic_slippage_max_extra_bps=settings.paper.dynamic_slippage_max_extra_bps,
    )
    broker.positions = {
        ticker: position.quantity for ticker, position in state.positions.items()
    }
    results: list[str] = []
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"
    open_tickers = set(state.positions)
    correlation_history_cache: dict[str, list[float]] = {}

    # V2.5: Calculate portfolio heat before trading
    portfolio_heat = _calculate_portfolio_heat(state, settings)

    # V2.5: Check kill switch before trading
    from trading_bot.safety.kill_switch import check_kill_switch_before_trade
    allowed, reason = check_kill_switch_before_trade(ledger)
    if not allowed:
        return [f"KILL_SWITCH: {reason}"]

    # V3.1: Circuit breaker — auto-halt on consecutive losses / max drawdown
    from trading_bot.safety.circuit_breaker import check_circuit_breakers
    cb_allowed, cb_reason = check_circuit_breakers(ledger, settings)
    if not cb_allowed:
        append_decision_event(
            log_path,
            {"command": "paper-trade", "status": "CIRCUIT_BREAKER", "reason": cb_reason},
        )
        return [f"CIRCUIT_BREAKER: {cb_reason}"]


    for symbol in (value.strip() for value in symbols if value.strip()):
        try:
            if _daily_loss_limit_hit(state, settings):
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": "daily loss limit",
                    },
                )
                results.append(f"{symbol} REJECTED daily loss limit")
                continue

            if _daily_order_limit_hit(ledger, settings):
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": "daily order limit",
                    },
                )
                results.append(f"{symbol} REJECTED daily order limit")
                continue

            if symbol in open_tickers:
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": "duplicate open ticker",
                    },
                )
                results.append(f"{symbol} REJECTED duplicate open ticker")
                continue

            cooldown_minutes = settings.risk.ticker_reentry_cooldown_minutes
            if cooldown_minutes > 0 and _recently_exited(symbol, state, cooldown_minutes):
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": "ticker re-entry cooldown",
                    },
                )
                results.append(f"{symbol} REJECTED ticker re-entry cooldown")
                continue

            signal, _, details = _build_signal_result(symbol, settings)
            if signal is None:
                details.update(
                    build_stacked_signal(
                        symbol,
                        signal,
                        details,
                        settings=settings.supermodel,
                    ).to_details()
                )
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "NO_SIGNAL",
                        **_paper_evidence_fields(details),
                    },
                )
                results.append(f"{symbol} NO_SIGNAL")
                continue

            counter_result = _evaluate_counter_thesis_for_signal(symbol, signal, settings)
            if counter_result is not None:
                _augment_details_with_counter_thesis(details, counter_result)
            stacked = build_stacked_signal(symbol, signal, details, settings=settings.supermodel)
            details.update(stacked.to_details())
            if stacked.decision == "block":
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": f"supermodel block (score={stacked.score:.2f})",
                        **_paper_evidence_fields(details),
                    },
                )
                results.append(f"{symbol} REJECTED supermodel block score={stacked.score:.2f}")
                continue

            # RL signals bypass the rule-based quality filter; the model itself
            # encodes entry criteria. Rule-based signals still require GREEN.
            # YELLOW signals may be accepted when the mean-reversion feature
            # flag is enabled and the intraday frame shows a valid setup.
            is_rl_signal = getattr(signal, "strategy_tag", "").startswith("rl_")
            is_v3_signal = getattr(signal, "strategy_tag", "").startswith("v3-")
            quality = _scan_quality(details)
            if not is_rl_signal and quality != "GREEN":
                yellow_accepted = False
                if settings.app.allow_yellow_mean_reversion and details.get("is_mean_reversion"):
                    yellow_accepted = True
                    details["yellow_mean_reversion"] = True
                if not yellow_accepted:
                    append_decision_event(
                        log_path,
                        {
                            "command": "paper-trade",
                            "ticker": symbol,
                            "status": "REJECTED",
                            "reason": "yellow signal",
                            **_paper_evidence_fields(details),
                        },
                    )
                    results.append(f"{symbol} REJECTED yellow signal")
                    continue

            # V2.5: Fetch ATR for volatility-adjusted sizing
            atr = _fetch_atr(symbol, settings) if settings.risk.use_atr_sizing else None

            # Apply strategy allocation multiplier (0.0 = skip, 0.5 = half size, 1.0 = full)
            from trading_bot.strategy.strategy_tracker import allocation_multiplier as _alloc_mult

            strategy_tag = getattr(signal, "strategy_tag", "")
            if strategy_tag:
                alloc = _alloc_mult(
                    Path(settings.app.log_dir),
                    strategy_tag,
                    settings=settings.strategy_tracker,
                )
                if alloc == 0.0:
                    append_decision_event(
                        log_path,
                        {
                            "command": "paper-trade",
                            "ticker": symbol,
                            "status": "REJECTED",
                            "reason": f"strategy={strategy_tag} allocation=0",
                            **_paper_evidence_fields(details),
                        },
                    )
                    results.append(f"{symbol} REJECTED strategy={strategy_tag} allocation=0")
                    continue
            else:
                alloc = 1.0

            min_conf = settings.app.min_entry_confluence_score
            if min_conf > 0.0 and not is_rl_signal and not is_v3_signal and not details.get("is_mean_reversion"):
                from trading_bot.strategy.setup_rules import compute_v25_confluence_score
                conf_score = compute_v25_confluence_score(details)
                details["confluence_score"] = conf_score
                if conf_score < min_conf:
                    append_decision_event(
                        log_path,
                        {
                            "command": "paper-trade",
                            "ticker": symbol,
                            "status": "REJECTED",
                            "reason": f"confluence_score={conf_score:.1f}<{min_conf}",
                            **_paper_evidence_fields(details),
                        },
                    )
                    results.append(f"{symbol} REJECTED low confluence {conf_score:.1f}<{min_conf}")
                    continue

            avg_correlation, max_avg_correlation = _correlation_context_for_candidate(
                symbol,
                state,
                settings,
                correlation_history_cache,
            )

            decision = evaluate_signal(
                signal=signal,
                account_equity=state.equity,
                open_tickers=open_tickers,
                portfolio_heat_pct=portfolio_heat,
                atr=atr,
                risk_settings=settings.risk,
                counter_thesis=counter_result,
                avg_correlation=avg_correlation,
                max_avg_correlation=max_avg_correlation,
            )
            if not decision.approved:
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": decision.reason,
                        **_paper_evidence_fields(details),
                        **(
                            {"counter_thesis": counter_result.to_dict()}
                            if counter_result is not None and counter_result.findings
                            else {}
                        ),
                    },
                )
                results.append(f"{symbol} REJECTED {decision.reason}")
                continue

            risk_approved_size = decision.position_size
            if alloc < 1.0:
                decision.position_size = max(1, int(decision.position_size * alloc))

            if details.get("yellow_mean_reversion"):
                decision.position_size = max(1, int(decision.position_size * settings.risk.yellow_allocation_pct))

            if details.get("is_half_size"):
                decision.position_size = max(1, int(decision.position_size * 0.5))

            if decision.position_size > risk_approved_size:
                decision.position_size = risk_approved_size
                details["position_size_capped"] = "risk_approved"

            sector_reason = _sector_concentration_exceeded(
                symbol,
                state,
                settings,
                pending_value=signal.entry_price * decision.position_size,
            )
            if sector_reason:
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": f"sector concentration: {sector_reason}",
                        **_paper_evidence_fields(details),
                    },
                )
                results.append(f"{symbol} REJECTED sector concentration: {sector_reason}")
                continue

            estimated_fill_price = broker.estimate_fill_price(
                OrderRequest(
                    ticker=symbol,
                    side="BUY",
                    order_type="market",
                    quantity=decision.position_size,
                    submitted_at=signal.timestamp,
                ),
                signal.entry_price,
            )
            estimated_total_cost = (estimated_fill_price * decision.position_size) + broker.fee_per_order
            if broker.cash < estimated_total_cost:
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": "insufficient cash",
                        **_paper_evidence_fields(details),
                    },
                )
                results.append(f"{symbol} REJECTED insufficient cash")
                continue

            if dry_run:
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "DRY_RUN",
                        "quantity": decision.position_size,
                        "fill_price": estimated_fill_price,
                        "cash_after": broker.cash - estimated_total_cost,
                        **_paper_evidence_fields(details),
                    },
                )
                results.append(
                    f"{symbol} DRY_RUN qty={decision.position_size} "
                    f"price={estimated_fill_price:.2f} cash_after={broker.cash - estimated_total_cost:.2f}"
                )
                continue

            fill = submit_signal_as_order(
                signal=signal,
                broker=broker,
                account_equity=state.equity,
                open_tickers=open_tickers,
                portfolio_heat_pct=portfolio_heat,
                atr=atr,
                risk_settings=settings.risk,
                counter_thesis=counter_result,
                position_size_override=decision.position_size,
            )
            if fill is None:
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": "broker rejected order",
                        **_paper_evidence_fields(details),
                    },
                )
                results.append(f"{symbol} REJECTED broker rejected order")
                continue

            strategy_tag = _trade_strategy_tag(signal, details) or ""
            ledger.record_fill(
                fill,
                side="BUY",
                strategy_tag=strategy_tag,
            )
            _persist_trade_to_db(fill, signal, settings, details)
            updated_state = _portfolio_state_from_broker(
                broker,
                signal,
                previous_state=state,
                fill_fees=fill.fees,
                filled_at=fill.filled_at,
            )
            ledger.save_portfolio_state(updated_state)
            ledger.record_equity_snapshot(updated_state, timestamp=fill.filled_at)
            state = updated_state
            open_tickers.add(symbol)
            # Recompute portfolio heat so the next symbol's heat check reflects
            # fills from this loop iteration. Without this, `portfolio_heat`
            # stays frozen at the pre-loop value (line ~498) and the Nth fill
            # uses stale heat from before fills 1..N-1.
            portfolio_heat = _calculate_portfolio_heat(state, settings)

            if strategy_tag:
                from trading_bot.strategy.strategy_tracker import record_entry as _rec_entry

                _rec_entry(
                    Path(settings.app.log_dir),
                    strategy_tag,
                    symbol,
                    fill.fill_price,
                    fill.filled_at or datetime.now(),
                )

            append_decision_event(
                log_path,
                {
                    "command": "paper-trade",
                    "ticker": fill.ticker,
                    "status": "FILLED",
                    "quantity": fill.quantity,
                    "fill_price": fill.fill_price,
                    "fees": fill.fees,
                    "cash": state.cash,
                    **_paper_evidence_fields(details),
                },
            )
            results.append(
                f"{symbol} FILLED qty={fill.quantity} price={fill.fill_price:.2f} cash={state.cash:.2f}"
                
            )
        except Exception as exc:
            append_decision_event(
                log_path,
                {"command": "paper-trade", "ticker": symbol, "status": "ERROR", "error": str(exc)},
            )
            results.append(f"{symbol} ERROR {exc}")

    return results


def _build_signal(symbol: str, settings: Settings) -> TradeSignal | None:
    signal, _ = _build_signal_with_reason(symbol, settings)
    return signal


def _build_signal_with_reason(symbol: str, settings: Settings) -> tuple[TradeSignal | None, str]:
    signal, reason, _ = _build_signal_result(symbol, settings)
    return signal, reason



def _copy_signal_with_confidence(signal: TradeSignal, confidence: float) -> TradeSignal:
    return TradeSignal(
        ticker=signal.ticker,
        timeframe=signal.timeframe,
        action=signal.action,
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        profit_target=signal.profit_target,
        risk_reward_ratio=signal.risk_reward_ratio,
        confidence=round(confidence, 4),
        reasons=list(signal.reasons),
        strategy_tag=getattr(signal, "strategy_tag", ""),
        timestamp=signal.timestamp,
        quality=getattr(signal, "quality", "GREEN"),
    )


def _fetch_hourly_alignment_frame(symbol: str, settings: Settings) -> pd.DataFrame | None:
    try:
        hourly_frame, hourly_valid = market_data.fetch_and_validate_bars(
            symbol,
            "3mo",
            "1h",
            settings.market_data,
        )
    except Exception as exc:
        logger.debug("hourly alignment fetch failed symbol=%s error=%s", symbol, exc)
        return None
    if not hourly_valid.valid or hourly_frame.empty:
        return None
    hourly_frame = _drop_trailing_zero_volume_bars(hourly_frame)
    hourly_frame = add_ema(hourly_frame, period=20, column_name="ema_20")
    hourly_frame = add_sma(hourly_frame, period=50, column_name="sma_50")
    return hourly_frame


def _apply_phase1_signal_quality(
    symbol: str,
    signal: TradeSignal | None,
    reason: str,
    details: dict,
    *,
    daily_frame: pd.DataFrame | None,
    intraday_frame: pd.DataFrame | None,
    hourly_frame: pd.DataFrame | None,
    settings: Settings,
    setup_name: str | None = None,
) -> tuple[TradeSignal | None, str, dict]:
    if signal is None or daily_frame is None or intraday_frame is None:
        return signal, reason, details

    signal_intraday_frame = _frame_through_timestamp(intraday_frame, signal.timestamp)
    signal_hourly_frame = (
        _frame_through_timestamp(hourly_frame, signal.timestamp)
        if hourly_frame is not None
        else None
    )
    verdict = evaluate_signal_quality(
        daily_frame=daily_frame,
        hourly_frame=signal_hourly_frame,
        intraday_frame=signal_intraday_frame,
        signal=signal,
        setup_name=setup_name,
        quality=str(details.get("quality", getattr(signal, "quality", ""))),
        required_count=getattr(settings.app, "min_timeframe_alignment", 1),
    )
    details.update(verdict.to_details())
    if not verdict.passed:
        return None, f"signal quality rejected: {verdict.reason}", details

    adapted_signal = adapt_signal_to_volatility_regime(
        signal,
        daily_frame,
        signal_intraday_frame,
        settings.risk,
    )
    details["adaptive_stop_loss"] = round(adapted_signal.stop_loss, 4)
    details["adaptive_profit_target"] = round(adapted_signal.profit_target, 4)
    details["adaptive_rr"] = round(adapted_signal.risk_reward_ratio, 4)
    return adapted_signal, reason, details


def _build_signal_result(symbol: str, settings: Settings) -> tuple[TradeSignal | None, str, dict]:
    # RL path: use RL model if enabled AND trained for this symbol.
    # If RL is enabled but the symbol isn't trained, fall through to V3.
    if getattr(settings, "rl", None) is not None and settings.rl.enabled:
        result = _build_rl_signal_result(symbol, settings)
        if result[0] is not None:
            return _apply_phase1_to_existing_signal(symbol, result[0], result[1], result[2], settings)
        if "rl_trained_symbols" not in result[2]:
            return result

    if getattr(settings, "strategy", None) is not None and settings.strategy.use_v3_signals:
        return _build_v3_signal_result(symbol, settings)

    # Legacy V2.5 path: validated data fetching + intraday signal engine.
    daily_frame, daily_valid = market_data.fetch_and_validate_bars(
        symbol,
        settings.market_data.daily_period,
        "1d",
        settings.market_data,
    )
    if not daily_valid.valid:
        return None, f"daily data validation failed: {daily_valid.reason}", {}

    intraday_frame, intraday_valid = market_data.fetch_and_validate_bars(
        symbol,
        settings.market_data.intraday_period,
        settings.market_data.intraday_interval,
        settings.market_data,
    )
    if not intraday_valid.valid:
        return None, f"intraday data validation failed: {intraday_valid.reason}", {}

    daily_frame = add_ema(daily_frame, period=20, column_name="ema_20")
    daily_frame = add_sma(daily_frame, period=50, column_name="sma_50")
    intraday_frame = _drop_trailing_zero_volume_bars(intraday_frame)
    intraday_frame["volume_avg_5"] = intraday_frame["volume"].rolling(5).mean()
    intraday_frame = add_atr(intraday_frame, period=settings.risk.atr_period)
    intraday_frame = add_rsi(intraday_frame, period=14)
    intraday_frame = add_bollinger_bands(intraday_frame, period=20, std_dev=2.0)
    intraday_frame = add_vwap(intraday_frame)
    signal, reason = generate_recent_signal_with_reason(
        symbol, daily_frame, intraday_frame,
        atr_stop_multiplier=settings.risk.atr_stop_multiplier,
        min_stop_distance_pct=settings.risk.min_stop_distance_pct,
    )
    detail_frame = _frame_through_timestamp(intraday_frame, signal.timestamp) if signal else intraday_frame
    details = _scan_details(daily_frame, detail_frame)
    if signal is not None:
        details["quality"] = getattr(signal, "quality", "GREEN")
    if settings.app.allow_yellow_mean_reversion:
        from trading_bot.strategy.setup_rules import is_valid_mean_reversion_setup
        details["is_mean_reversion"] = is_valid_mean_reversion_setup(intraday_frame)
    hourly_frame = _fetch_hourly_alignment_frame(symbol, settings)
    return _apply_phase1_signal_quality(
        symbol,
        signal,
        reason,
        details,
        daily_frame=daily_frame,
        intraday_frame=detail_frame,
        hourly_frame=hourly_frame,
        settings=settings,
    )


def _apply_phase1_to_existing_signal(
    symbol: str,
    signal: TradeSignal,
    reason: str,
    details: dict,
    settings: Settings,
) -> tuple[TradeSignal | None, str, dict]:
    daily_frame, daily_valid = market_data.fetch_and_validate_bars(
        symbol,
        settings.market_data.daily_period,
        "1d",
        settings.market_data,
    )
    if not daily_valid.valid:
        return None, f"daily data validation failed: {daily_valid.reason}", details

    intraday_frame, intraday_valid = market_data.fetch_and_validate_bars(
        symbol,
        settings.market_data.intraday_period,
        settings.market_data.intraday_interval,
        settings.market_data,
    )
    if not intraday_valid.valid:
        return None, f"intraday data validation failed: {intraday_valid.reason}", details

    daily_frame = add_ema(daily_frame, period=20, column_name="ema_20")
    daily_frame = add_sma(daily_frame, period=50, column_name="sma_50")
    intraday_frame = _drop_trailing_zero_volume_bars(intraday_frame)
    intraday_frame = intraday_frame.copy()
    intraday_frame["volume_avg_5"] = intraday_frame["volume"].rolling(5).mean()
    intraday_frame = add_atr(intraday_frame, period=settings.risk.atr_period)
    intraday_frame = add_rsi(intraday_frame, period=14)
    intraday_frame = add_bollinger_bands(intraday_frame, period=20, std_dev=2.0)
    intraday_frame = add_vwap(intraday_frame)
    details.update(_scan_details(daily_frame, intraday_frame))
    details["quality"] = getattr(signal, "quality", "GREEN")
    hourly_frame = _fetch_hourly_alignment_frame(symbol, settings)
    return _apply_phase1_signal_quality(
        symbol,
        signal,
        reason,
        details,
        daily_frame=daily_frame,
        intraday_frame=intraday_frame,
        hourly_frame=hourly_frame,
        settings=settings,
    )


def _build_rl_signal_result(symbol: str, settings: Settings) -> tuple[TradeSignal | None, str, dict]:
    """RL-based signal generation using trained DRL agent.

    Loads one or more trained models (via ``model_paths`` or ``model_path``)
    and uses ensemble prediction when multiple models cover the same symbol.
    Converts BUY actions to TradeSignal objects with risk management parameters.
    """
    from trading_bot.rl.agent import RLAgent

    model_paths = _resolve_rl_model_paths(symbol, settings)
    if not model_paths:
        return None, "no RL models configured", {}

    has_meta = any(rl_model_symbols(p) for p in model_paths)
    if not has_meta:
        return None, f"RL model metadata missing or empty: {model_paths[0] if model_paths else 'none'}", {}

    # Load matching models and collect predictions
    predictions: list[dict] = []
    target_intraday_frame = None
    rl_trained_symbols: list[str] = []
    for mp in model_paths:
        trained_symbols = rl_model_symbols(mp) or []
        if not trained_symbols:
            logger.debug("RL model skipped path=%s metadata missing or empty", mp)
            continue
        for ts in trained_symbols:
            if ts not in rl_trained_symbols:
                rl_trained_symbols.append(ts)

        symbol_upper = symbol.upper().strip()
        is_trained = symbol_upper in trained_symbols
        if not is_trained and not getattr(settings.rl, "allow_untrained_symbol_inference", False):
            logger.debug("RL model skipped path=%s symbol=%s not in metadata", mp, symbol_upper)
            continue

        try:
            agent = RLAgent.load(model_path=mp)
        except Exception as e:
            logger.debug("RL model load failed path=%s error=%s", mp, e)
            continue

        symbols_for_inference = list(trained_symbols)
        if not is_trained:
            symbols_for_inference.append(symbol_upper)

        daily_frames: dict[str, Any] = {}
        intraday_frames: dict[str, Any] = {}
        for sym in symbols_for_inference:
            daily_frame, daily_valid = market_data.fetch_and_validate_bars(
                sym, settings.market_data.daily_period, "1d", settings.market_data,
            )
            if not daily_valid.valid:
                break
            intraday_frame, intraday_valid = market_data.fetch_and_validate_bars(
                sym, settings.market_data.intraday_period,
                settings.market_data.intraday_interval, settings.market_data,
            )
            if not intraday_valid.valid:
                break
            daily_frames[sym] = daily_frame
            intraday_frames[sym] = intraday_frame
        else:
            daily_frame = daily_frames[symbol_upper]
            intraday_frame = intraday_frames[symbol_upper]
            daily_frame = daily_frame.copy()
            daily_frame = add_ema(daily_frame, period=20, column_name="ema_20")
            daily_frame = add_sma(daily_frame, period=50, column_name="sma_50")
            daily_frame = add_atr(daily_frame, period=settings.risk.atr_period)
            daily_frame = add_bollinger_bands(daily_frame, period=20)
            intraday_frame = _drop_trailing_zero_volume_bars(intraday_frame)
            intraday_frame = intraday_frame.copy()
            intraday_frame["volume_avg_5"] = intraday_frame["volume"].rolling(5).mean()
            intraday_frame = add_rsi(intraday_frame, period=14)
            intraday_frame = add_atr(intraday_frame, period=settings.risk.atr_period)
            action, confidence = agent.predict_signal(
                daily_frame=daily_frame,
                ticker=symbol_upper,
                portfolio_weight=0.0,
                unrealized_pnl_pct=0.0,
                cash_ratio=1.0,
                symbols=symbols_for_inference,
                market_frames=daily_frames,
            )
            predictions.append({
                "action": action,
                "confidence": float(confidence),
                "is_trained": is_trained,
                "model": str(mp),
            })
            target_intraday_frame = intraday_frame
            continue
        # One or more frames failed validation for this model — skip it
        logger.debug("RL model skipped path=%s data validation failed", mp)

    if not predictions:
        if rl_trained_symbols:
            return None, f"RL model not trained for {symbol.upper()}", {
                "rl_trained_symbols": rl_trained_symbols,
                "rl_untrained_symbol": True,
                "rl_models": 0,
            }
        return None, "RL agent failed: no models produced valid predictions", {}

    # Ensemble: majority action, average confidence
    action_votes: dict[int, int] = {}
    total_conf = 0.0
    trained_count = 0
    for p in predictions:
        act = int(p["action"])
        action_votes[act] = action_votes.get(act, 0) + 1
        total_conf += p["confidence"]
        if p["is_trained"]:
            trained_count += 1
    avg_confidence = total_conf / len(predictions)
    model_count = len(predictions)
    top_votes = max(action_votes.values())
    top_actions = sorted(action for action, count in action_votes.items() if count == top_votes)
    if len(top_actions) > 1:
        _persist_rl_prediction_to_db(symbol, 0, avg_confidence, settings)
        return None, f"RL ensemble action tie ({top_actions})", {
            "rl_action": 0,
            "rl_confidence": round(avg_confidence, 3),
            "rl_trained_symbols": rl_trained_symbols,
            "rl_untrained_symbol": symbol.upper() not in rl_trained_symbols,
            "rl_models": model_count,
            "rl_vote_tie": top_actions,
        }
    best_action = top_actions[0]

    # Apply untrained penalty. Unknown symbols must clear the normal threshold
    # after confidence is discounted, not receive an easier threshold.
    confidence_mult = settings.rl.untrained_confidence_threshold_multiplier if hasattr(settings.rl, 'untrained_confidence_threshold_multiplier') else 0.8
    effective_confidence = avg_confidence
    effective_threshold = settings.rl.action_confidence_threshold
    if trained_count == 0:
        effective_confidence *= confidence_mult

    if effective_confidence < effective_threshold or best_action != 1:
        _persist_rl_prediction_to_db(symbol, best_action, avg_confidence, settings)
        details: dict[str, object] = {
            "rl_action": best_action,
            "rl_confidence": round(avg_confidence, 3),
            "rl_effective_confidence": round(effective_confidence, 3),
            "rl_trained_symbols": rl_trained_symbols,
            "rl_untrained_symbol": symbol.upper() not in rl_trained_symbols,
            "rl_models": model_count,
        }
        if best_action == 0:
            return None, f"RL agent predicts HOLD (confidence={avg_confidence:.2f})", details
        elif best_action == 2:
            return None, f"RL agent predicts SELL (confidence={avg_confidence:.2f})", details
        return None, f"RL confidence {effective_confidence:.2f} below threshold {effective_threshold}", details

    current_price = 0.0
    if target_intraday_frame is not None and not target_intraday_frame.empty and "close" in target_intraday_frame.columns:
        current_price = float(target_intraday_frame["close"].iloc[-1])
    if not math.isfinite(current_price) or current_price <= 0:
        _persist_rl_prediction_to_db(symbol, best_action, avg_confidence, settings)
        return None, "RL current price unavailable", {
            "rl_action": best_action,
            "rl_confidence": round(avg_confidence, 3),
            "rl_effective_confidence": round(effective_confidence, 3),
            "rl_trained_symbols": rl_trained_symbols,
            "rl_untrained_symbol": symbol.upper() not in rl_trained_symbols,
            "rl_models": model_count,
        }

    atr_col = f"atr_{settings.risk.atr_period}"
    atr = (
        float(target_intraday_frame[atr_col].iloc[-1])
        if target_intraday_frame is not None
        and not target_intraday_frame.empty
        and atr_col in target_intraday_frame.columns
        else current_price * 0.02
    )
    if not math.isfinite(atr) or atr <= 0:
        atr = current_price * 0.02
    stop_distance = atr * settings.risk.atr_multiplier
    stop_loss = current_price - stop_distance
    target = current_price + (stop_distance * settings.risk.min_reward_risk_ratio)

    if not trained_count:
        details = {
            "rl_action": best_action,
            "rl_confidence": round(avg_confidence, 3),
            "rl_effective_confidence": round(effective_confidence, 3),
            "rl_untrained_symbol": True,
            "rl_models": model_count,
        }
        signal = TradeSignal(
            ticker=symbol.upper(),
            timeframe="intraday",
            action="BUY",
            entry_price=current_price,
            stop_loss=stop_loss,
            profit_target=target,
            risk_reward_ratio=settings.risk.min_reward_risk_ratio,
            confidence=effective_confidence,
            reasons=[f"RL ensemble ({model_count} models)"],
            strategy_tag=f"rl_{settings.rl.agent_type}",
            timestamp=datetime.now(timezone.utc),
        )
        _persist_rl_prediction_to_db(symbol, best_action, avg_confidence, settings)
        return signal, "rl ensemble untrained", details

    details = {
        "rl_action": best_action,
        "rl_confidence": round(avg_confidence, 3),
        "rl_effective_confidence": round(effective_confidence, 3),
        "rl_models": model_count,
    }
    signal = TradeSignal(
        ticker=symbol.upper(),
        timeframe="intraday",
        action="BUY",
        entry_price=current_price,
        stop_loss=stop_loss,
        profit_target=target,
        risk_reward_ratio=settings.risk.min_reward_risk_ratio,
        confidence=avg_confidence,
        reasons=[f"RL ensemble ({model_count} models)"],
        strategy_tag=f"rl_{settings.rl.agent_type}",
        timestamp=datetime.now(timezone.utc),
    )
    _persist_rl_prediction_to_db(symbol, best_action, avg_confidence, settings)
    return signal, "rl ensemble approved", details


def _resolve_rl_model_paths(symbol: str, settings: Settings) -> list[Path]:
    """Resolve which RL model(s) to use for a given symbol.

    Returns an ordered list of model paths that cover the target symbol.
    When ``model_paths`` is configured, uses those paths. Otherwise falls
    back to the single ``model_path``.
    """
    base = Path(settings.app.log_dir).parent
    paths = settings.rl.model_paths or []
    if not paths:
        default = base / settings.rl.model_path
        return [default] if default.exists() else []

    matching: list[Path] = []
    for p in paths:
        full = base / p
        if not full.exists():
            continue
        symbols = rl_model_symbols(full)
        if symbols and symbol.upper() in symbols:
            matching.append(full)

    if matching:
        return matching

    default = base / settings.rl.model_path
    return [default] if default.exists() else []


def _build_v3_signal_result(
    symbol: str,
    settings: Settings,
    daily_frame: pd.DataFrame | None = None,
    intraday_frame: pd.DataFrame | None = None,
):
    """V3 strategy path: regime detection + 5-factor confluence scoring.

    Produces a :class:`TradeSignal` adapted from a
    :class:`StrategySelection`. Falls back to the legacy path's details
    structure so downstream scan/report code is unchanged.

    When *daily_frame* and *intraday_frame* are provided, the internal
    data fetches are skipped — used by the parallel signal resolver to
    share a single fetch across sources.
    """
    from trading_bot.strategy.strategy_selector import StrategySelector

    if daily_frame is None or intraday_frame is None:
        daily_frame, daily_valid = market_data.fetch_and_validate_bars(
            symbol,
            settings.market_data.daily_period,
            "1d",
            settings.market_data,
        )
        if not daily_valid.valid:
            return None, f"daily data validation failed: {daily_valid.reason}", {}

        intraday_frame, intraday_valid = market_data.fetch_and_validate_bars(
            symbol,
            settings.market_data.intraday_period,
            settings.market_data.intraday_interval,
            settings.market_data,
        )
        if not intraday_valid.valid:
            return None, f"intraday data validation failed: {intraday_valid.reason}", {}

    # detect_market_regime requires ema_20, sma_50, atr_14, and bb_* on daily frame.
    daily_frame = daily_frame.copy()
    if "ema_20" not in daily_frame.columns:
        daily_frame = add_ema(daily_frame, period=20, column_name="ema_20")
    if "sma_50" not in daily_frame.columns:
        daily_frame = add_sma(daily_frame, period=50, column_name="sma_50")
    atr_column = f"atr_{settings.risk.atr_period}"
    if atr_column not in daily_frame.columns:
        daily_frame = add_atr(daily_frame, period=settings.risk.atr_period)
    if "bb_width" not in daily_frame.columns:
        daily_frame = add_bollinger_bands(daily_frame, period=20)
    intraday_frame = _drop_trailing_zero_volume_bars(intraday_frame)
    intraday_frame = intraday_frame.copy()
    intraday_frame["volume_avg_5"] = intraday_frame["volume"].rolling(5).mean()
    intraday_frame = add_rsi(intraday_frame, period=14)
    intraday_frame = add_atr(intraday_frame, period=settings.risk.atr_period)
    intraday_frame = add_bollinger_bands(intraday_frame, period=20)
    intraday_frame = add_vwap(intraday_frame)

    selector = StrategySelector(risk_tolerance=settings.strategy.risk_tolerance)
    selector.min_confidence = settings.strategy.min_confidence
    selector.atr_stop_multiplier = settings.risk.atr_stop_multiplier
    selector.min_stop_distance_pct = settings.risk.min_stop_distance_pct
    selection = selector.select_strategy(symbol, daily_frame, intraday_frame)

    details = _scan_details(daily_frame, intraday_frame)
    if not selection.should_trade or selection.signal_score is None:
        return None, selection.reason, details

    from trading_bot.strategy.strategy_selector import selection_to_signal

    signal = selection_to_signal(symbol, selection, intraday_frame)
    if signal is None:
        return None, "v3 signal adaptation failed", details

    # Set quality for V3 signals (always GREEN since selection passed all gates)
    signal.quality = "GREEN"

    # Surface confluence component scores in scan --why output.
    score = selection.signal_score
    details["v3_total_score"] = round(score.total_score, 2)
    details["v3_confidence"] = score.confidence
    details["v3_regime"] = selection.regime.value if selection.regime else None
    details["v3_setup"] = selection.setup_name
    details["quality"] = signal.quality
    if settings.app.allow_yellow_mean_reversion:
        details["is_mean_reversion"] = selection.strategy_type == "mean_reversion"
    hourly_frame = _fetch_hourly_alignment_frame(symbol, settings)
    return _apply_phase1_signal_quality(
        symbol,
        signal,
        "v3 approved",
        details,
        daily_frame=daily_frame,
        intraday_frame=intraday_frame,
        hourly_frame=hourly_frame,
        settings=settings,
        setup_name=selection.setup_name,
    )


def _drop_trailing_zero_volume_bars(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "volume" not in frame.columns:
        return frame.copy(deep=True)

    end = len(frame)
    while end > 0:
        volume = _finite_float(frame.iloc[end - 1].get("volume"))
        if volume is not None and volume > 0:
            break
        end -= 1
    return frame.iloc[:end].copy(deep=True)


def _frame_through_timestamp(frame: pd.DataFrame, timestamp: datetime) -> pd.DataFrame:
    if frame.empty or "timestamp" not in frame.columns:
        return frame
    ts = timestamp
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    frame_ts = frame["timestamp"]
    if hasattr(frame_ts, "dt") and frame_ts.dt.tz is not None:
        frame_ts = frame_ts.dt.tz_localize(None)
    matches = frame.index[frame_ts == ts].tolist()
    if not matches:
        return frame
    return frame.iloc[: matches[-1] + 1]


def _scan_details(daily_frame, intraday_frame) -> dict[str, float | int]:
    details: dict[str, float | int] = {}
    if not daily_frame.empty:
        latest_daily = daily_frame.iloc[-1]
        _set_detail(details, "daily_close", latest_daily.get("close"))
        _set_detail(details, "ema_20", latest_daily.get("ema_20"))
        _set_detail(details, "sma_50", latest_daily.get("sma_50"))

    if not intraday_frame.empty:
        latest_intraday = intraday_frame.iloc[-1]
        _set_detail(details, "intraday_close", latest_intraday.get("close"))
        if "high" in intraday_frame.columns and len(intraday_frame) > 1:
            recent_highs = intraday_frame.tail(5).iloc[:-1]["high"]
            high_values = [_finite_float(value) for value in recent_highs.tolist()]
            valid_highs = [value for value in high_values if value is not None]
            if valid_highs:
                details["range_high"] = round(max(valid_highs), 2)
        volume = _finite_float(latest_intraday.get("volume"))
        if volume is not None:
            details["volume"] = int(volume)
        average_volume = _finite_float(latest_intraday.get("volume_avg_5"))
        if average_volume is not None:
            details["volume_avg"] = round(average_volume, 2)
        if volume is not None and average_volume is not None and average_volume > 0:
            details["volume_ratio"] = round(volume / average_volume, 2)

    return details


def _set_detail(details: dict[str, float | int], key: str, value: object) -> None:
    numeric = _finite_float(value)
    if numeric is not None:
        details[key] = round(numeric, 2)


def _finite_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _format_scan_details(details: dict[str, object]) -> str:
    parts: list[str] = []
    for key in (
        "daily_close",
        "ema_20",
        "sma_50",
        "intraday_close",
        "range_high",
        "volume",
        "volume_avg",
        "volume_ratio",
    ):
        value = details.get(key)
        if value is None:
            continue
        if key == "volume":
            parts.append(f"{key}={int(value)}")
        else:
            numeric = _finite_float(value)
            if numeric is not None:
                parts.append(f"{key}={numeric:.2f}")

    # V3 strategy layer details (may contain strings, not just floats)
    for key in ("v3_total_score", "v3_confidence", "v3_regime", "v3_setup"):
        value = details.get(key)
        if value is not None:
            parts.append(f"{key}={value}")

    for key in (
        "mtf_aligned",
        "mtf_required",
        "mtf_regime",
        "entry_volume_ratio",
        "entry_range_ratio",
        "adaptive_rr",
    ):
        value = details.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    if details.get("mtf_passed") is not None:
        parts.append(f"mtf_passed={details.get('mtf_passed')}")
    if details.get("entry_timing_passed") is not None:
        parts.append(f"entry_timing_passed={details.get('entry_timing_passed')}")

    # V3: counter-thesis summary surfaced in scan --why output.
    for key in (
        "counter_thesis_severity",
        "counter_thesis_findings",
        "counter_thesis_confidence",
    ):
        value = details.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    if details.get("counter_thesis_block"):
        parts.append("counter_thesis_block=true")

    if details.get("supermodel_decision"):
        parts.append(
            f"supermodel={details.get('supermodel_decision')}"
            f":{details.get('supermodel_score')}"
        )
    if details.get("supermodel_layers"):
        parts.append(f"supermodel_layers={details.get('supermodel_layers')}")

    return f" {' '.join(parts)}" if parts else ""


def _scan_quality(details: dict[str, float | int]) -> str:
    # Use signal-set quality as source of truth (set by signal engine or V3 path)
    if "quality" in details:
        return str(details["quality"])
    # Legacy fallback: derive from close/range_high/volume
    close = details.get("intraday_close")
    range_high = details.get("range_high")
    volume_ratio = details.get("volume_ratio")
    if (
        close is not None
        and range_high is not None
        and volume_ratio is not None
        and float(close) > float(range_high)
        and float(volume_ratio) >= 1.0
    ):
        return "GREEN"
    return "YELLOW"


def _scan_row_sort_key(row: dict[str, object]) -> tuple[int, int, int, float, float, float, float, float]:
    status_order = {
        "APPROVED": 3,
        "REJECTED": 2,
        "NO_SIGNAL": 1,
        "ERROR": 0,
    }
    quality_order = {"GREEN": 2, "YELLOW": 1}
    freshness_order = {"fresh": 2, "stale": 1}
    details = row.get("details")
    detail_map = details if isinstance(details, dict) else {}
    relative_volume = _finite_float(
        detail_map.get("entry_volume_ratio", detail_map.get("volume_ratio"))
    ) or 0.0
    return (
        status_order.get(str(row.get("status", "")), -1),
        quality_order.get(str(row.get("quality", "")), 0),
        freshness_order.get(str(row.get("freshness", "")), 0),
        relative_volume,
        float(row.get("rr", 0.0) or 0.0),
        float(row.get("confidence", -1.0) or -1.0),
        -float(row.get("risk", 0.0) or 0.0),
    )


def _daily_loss_limit_hit(state: PortfolioState, settings: Settings) -> bool:
    limit = state.equity * settings.risk.max_daily_risk_pct
    return state.realized_pnl <= -limit


def _daily_order_limit_hit(ledger: PortfolioLedger, settings: Settings) -> bool:
    today = datetime.now().date().isoformat()
    orders = [
        row
        for row in ledger.list_order_rows()
        if row["side"] == "BUY" and str(row["filled_at"]).startswith(today)
    ]
    return len(orders) >= settings.risk.max_daily_orders


def _scan_now(signal_timestamp: datetime) -> datetime:
    """Return the current time, timezone-matched to *signal_timestamp*.

    Always returns a **timezone-aware** ``datetime`` so comparisons with
    aware timestamps never raise ``TypeError``.
    """
    from datetime import timezone

    now = datetime.now(timezone.utc)
    if signal_timestamp.tzinfo is not None:
        try:
            now = now.astimezone(signal_timestamp.tzinfo)
        except (ValueError, OSError) as e:
            logger.debug("Orchestrator error: %s", e)
    return now


def _portfolio_state_from_broker(
    broker: PaperBroker,
    signal,
    previous_state: PortfolioState,
    fill_fees: float,
    filled_at: datetime | None = None,
) -> PortfolioState:
    positions: dict[str, Position] = {}
    for ticker, quantity in broker.positions.items():
        if quantity <= 0:
            continue
        prior = previous_state.positions.get(ticker)
        if prior is not None:
            positions[ticker] = prior.model_copy(update={"quantity": quantity})
            continue
        positions[ticker] = Position(
            ticker=ticker,
            quantity=quantity,
            average_cost=signal.entry_price,
            stop_loss=signal.stop_loss,
            profit_target=signal.profit_target,
            entry_at=filled_at,
            strategy_tag=getattr(signal, "strategy_tag", ""),
        )
    equity = broker.cash + sum(
        position.quantity * position.average_cost for position in positions.values()
    )
    return PortfolioState(
        cash=round(broker.cash, 2),
        equity=round(equity, 2),
        positions=positions,
        realized_pnl=round(previous_state.realized_pnl - fill_fees, 2),
        unrealized_pnl=0.0,
    )


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_us_market_open(now: datetime | None = None) -> bool:
    """Return True when the NYSE/NASDAQ are currently in regular trading hours.

    Uses US Eastern timezone.  Market hours are Monday-Friday 09:30-16:00 ET.
    Returns False on weekends and outside those hours.
    """
    if now is None:
        now = _scan_now(datetime.now(timezone.utc))
    # Convert to US Eastern
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        et = pytz.timezone("America/New_York")
    et_now = now.astimezone(et)

    # Weekend check (0=Monday, 6=Sunday)
    if et_now.weekday() >= 5:
        return False

    hour = et_now.hour
    minute = et_now.minute
    time_minutes = hour * 60 + minute

    # Market open 09:30 (570), close 16:00 (960)
    return 570 <= time_minutes < 960


def _market_data_status(signal_timestamp: datetime, interval: str, max_age_minutes: int = 30) -> str:
    signal_timestamp = _ensure_aware(signal_timestamp)
    age = _scan_now(signal_timestamp) - signal_timestamp
    # When the market is closed and data is roughly one trading day old
    # (12-24 hours), treat it as fresh — data from yesterday's close is
    # acceptable.  Short-age data (e.g. 40 min) is still checked against
    # the normal threshold so we don't silently accept stale intraday bars.
    if not _is_us_market_open() and timedelta(hours=12) <= age <= timedelta(hours=24):
        return "fresh"
    return "stale" if age > timedelta(minutes=max_age_minutes) else "fresh"


def _market_data_age(signal_timestamp: datetime) -> str:
    signal_timestamp = _ensure_aware(signal_timestamp)
    age = max(_scan_now(signal_timestamp) - signal_timestamp, timedelta())
    minutes = int(age.total_seconds() // 60)
    return f"{minutes}m"


def _stale_after(interval: str) -> timedelta:
    # ponytail: small parser for common yfinance-style intervals; fallback stays conservative.
    unit = interval[-1:]
    value_text = interval[:-1]
    try:
        value = int(value_text)
    except ValueError:
        return timedelta(minutes=15)

    if unit == "m":
        return timedelta(minutes=value * 3)
    if unit == "h":
        return timedelta(hours=value * 3)
    if unit == "d":
        return timedelta(days=value * 3)
    return timedelta(minutes=15)


def _fetch_atr(symbol: str, settings: Settings) -> float | None:
    """Fetch 14-period ATR for volatility-adjusted position sizing.

    Returns None if ATR cannot be calculated (e.g., insufficient data).
    """
    try:
        frame = market_data.fetch_bars(
            symbol,
            settings.market_data.daily_period,
            "1d",
            settings=settings.market_data,
        )
        if frame.empty or len(frame) < settings.risk.atr_period + 5:
            return None

        # Check if we have required columns for ATR
        required_columns = {"high", "low", "close"}
        if not required_columns.issubset(frame.columns):
            return None

        atr_frame = add_atr(frame, period=settings.risk.atr_period)
        atr_series = atr_frame[f"atr_{settings.risk.atr_period}"].dropna()
        if atr_series.empty:
            return None

        return float(atr_series.iloc[-1])
    except Exception:
        return None


def _evaluate_counter_thesis_for_signal(symbol: str, signal: Any, settings: Settings) -> Any:
    """Fetch counter-thesis context and run the checks against ``signal``.

    Returns None when the feature is disabled (so the risk manager skips it
    entirely). When the feature is enabled but the context cannot be fetched
    or built, returns an empty non-blocking result: a data outage must not
    silently become a kill switch.
    """
    if not settings.counter_thesis.enabled:
        return None
    from trading_bot.strategy.counter_thesis import (
        evaluate_counter_thesis,
        fetch_counter_thesis_context,
    )

    ctx = fetch_counter_thesis_context(
        symbol,
        signal,
        settings.market_data,
        settings.risk.atr_period,
    )
    return evaluate_counter_thesis(ctx, signal, settings.counter_thesis)


def _augment_details_with_counter_thesis(details: dict[str, object], result) -> None:
    """Surface counter-thesis summary fields on the scan --why output."""
    details["counter_thesis_severity"] = result.overall_severity
    details["counter_thesis_findings"] = len(result.findings)
    details["counter_thesis_block"] = result.block_trade
    details["counter_thesis_confidence"] = round(result.confidence_multiplier, 2)


def _evaluate_counter_thesis_for_position(
    ticker: str,
    position,
    intraday_frame,
    settings: Settings,
):
    """Build counter-thesis context for an open position and evaluate.

    Used by manage-positions for exit-side analysis: when the BUY thesis
    is broken (blocked), the position exits early as a 'counter-thesis'
    exit. Uses the already-fetched intraday frame + a fresh daily fetch.

    Returns None when the feature is disabled; returns a non-blocking
    empty result when data is unavailable (never blocks on missing data).
    """
    if not settings.counter_thesis.enabled or not settings.counter_thesis.exit_on_block:
        return None
    from trading_bot.strategy.counter_thesis import (
        build_counter_thesis_context,
        evaluate_counter_thesis,
    )

    try:
        daily_frame = market_data.fetch_bars(
            ticker,
            settings.market_data.daily_period,
            "1d",
            settings=settings.market_data,
        )
    except Exception:
        return evaluate_counter_thesis(None, position, settings.counter_thesis)

    if daily_frame is None or daily_frame.empty:
        return evaluate_counter_thesis(None, position, settings.counter_thesis)

    ctx = build_counter_thesis_context(
        symbol=ticker,
        signal=position,  # duck-typed: only strategy_tag is read
        daily_frame=daily_frame,
        intraday_frame=intraday_frame,
        atr_period=settings.risk.atr_period,
    )
    return evaluate_counter_thesis(ctx, position, settings.counter_thesis)


def _calculate_portfolio_heat(state: PortfolioState, settings: Settings) -> float:
    """Calculate current portfolio heat (unrealized loss % of equity).

    When a fresh price cannot be fetched, falls back to the position's
    stop-loss price (worst-case) instead of average_cost, so that heat
    is maximised and ``max_portfolio_heat_pct`` will correctly block
    new trades even when market data is unavailable.
    """
    if not state.positions or state.equity <= 0:
        return 0.0

    # Fetch latest prices for all positions
    latest_prices: dict[str, float] = {}
    for ticker in state.positions:
        pos = state.positions[ticker]
        try:
            frame, validation = market_data.fetch_and_validate_bars(
                ticker,
                settings.market_data.intraday_period,
                settings.market_data.intraday_interval,
                settings.market_data,
            )
            if (
                validation.valid
                and not frame.empty
                and "close" in frame.columns
            ):
                last_price = _finite_float(frame.iloc[-1]["close"])
                if last_price is not None:
                    latest_prices[ticker] = last_price
                    continue
        except Exception:
            logger.debug("Failed to fetch price for portfolio heat calculation")
        # Fail-closed: use stop-loss as fallback (worst-case heat).
        # If no stop-loss is defined, fall back to average_cost.
        fallback = pos.stop_loss if pos.stop_loss is not None else pos.average_cost
        latest_prices[ticker] = fallback

    return compute_portfolio_heat(state.positions, latest_prices, state.equity)


def _persist_scan_results_to_db(candidate_rows: list[dict], settings: Settings) -> None:
    try:
        from trading_bot.db.session import init_db, make_session_factory, get_session
        from trading_bot.db.repositories import upsert_scan_feature, upsert_scan_result
        engine = init_db(settings)
        session_factory = make_session_factory(engine)
        session = get_session(session_factory)
        try:
            for row in candidate_rows:
                ticker = row.get("ticker", "")
                status = row.get("status", "")
                if status == "APPROVED":
                    action = "BUY"
                    confidence = float(row.get("confidence", 0.0))
                    reasons = row.get("reasons")
                    if isinstance(reasons, list):
                        reasons = [str(r) for r in reasons]
                    else:
                        reasons = None
                    score = None
                    strategy_tag = None
                    details_dict = _scan_row_details_for_persistence(row)
                    if isinstance(details_dict, dict):
                        score = details_dict.get("v3_total_score")
                        strategy_tag = details_dict.get("rl_agent_type") or details_dict.get("v3_strategy")
                    scan_result = upsert_scan_result(
                        session=session,
                        ticker=ticker.upper(),
                        action=action,
                        confidence=confidence,
                        score=score,
                        strategy_tag=strategy_tag,
                        reasons=reasons,
                        details=details_dict,
                    )
                    _persist_scan_feature_row(session, scan_result.id, row, action, confidence, strategy_tag)
                elif status == "NO_SIGNAL":
                    reason = row.get("reason", "no signal")
                    scan_result = upsert_scan_result(
                        session=session,
                        ticker=ticker.upper(),
                        action="HOLD",
                        confidence=0.0,
                        reasons=[str(reason)],
                        details=_scan_row_details_for_persistence(row),
                    )
                    _persist_scan_feature_row(session, scan_result.id, row, "HOLD", 0.0, None)
                elif status == "REJECTED":
                    reason = row.get("reason", "rejected")
                    scan_result = upsert_scan_result(
                        session=session,
                        ticker=ticker.upper(),
                        action="HOLD",
                        confidence=0.0,
                        reasons=[str(reason)],
                        details=_scan_row_details_for_persistence(row),
                    )
                    _persist_scan_feature_row(session, scan_result.id, row, "HOLD", 0.0, None)
                elif status == "ERROR":
                    error = row.get("error", "unknown error")
                    upsert_scan_result(
                        session=session,
                        ticker=ticker.upper(),
                        action="HOLD",
                        confidence=0.0,
                        reasons=[str(error)],
                    )
        finally:
            session.close()
            engine.dispose()
    except Exception:
        logger.debug("Failed to persist scan results to database")


def _persist_scan_feature_row(
    session,
    scan_result_id: int,
    row: dict,
    action: str,
    confidence: float,
    strategy_tag: str | None,
) -> None:
    from trading_bot.db.repositories import upsert_scan_feature

    details = _scan_row_details_for_persistence(row) or {}
    upsert_scan_feature(
        session=session,
        scan_result_id=scan_result_id,
        ticker=str(row.get("ticker", "")).upper(),
        status=str(row.get("status", "")),
        action=action,
        confidence=confidence,
        quality=str(row.get("quality", "")) or None,
        freshness=str(row.get("freshness", "")) or None,
        market_age_minutes=_market_age_minutes(row.get("age")),
        market_regime=str(details.get("mtf_regime", details.get("v3_regime", ""))) or None,
        strategy_tag=strategy_tag,
        consensus=str(details.get("consensus", "")) or None,
        v3_total_score=_finite_float(details.get("v3_total_score")),
        supermodel_score=_finite_float(row.get("supermodel_score", details.get("supermodel_score"))),
        mtf_aligned=_safe_int(details.get("mtf_aligned")),
        entry_volume_ratio=_finite_float(details.get("entry_volume_ratio")),
        entry_range_ratio=_finite_float(details.get("entry_range_ratio")),
        adaptive_rr=_finite_float(details.get("adaptive_rr")),
    )


def _market_age_minutes(value: object) -> int | None:
    text = str(value or "").strip().lower()
    if text.endswith("m"):
        try:
            return int(text[:-1])
        except ValueError:
            return None
    return None


def _safe_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scan_row_details_for_persistence(row: dict) -> dict | None:
    details = row.get("details")
    if isinstance(details, dict):
        return details
    compact = {
        key: row[key]
        for key in (
            "supermodel_decision",
            "supermodel_score",
        )
        if key in row
    }
    return compact or None


def _persist_trade_to_db(
    fill,
    signal,
    settings: Settings,
    details: dict | None = None,
) -> None:
    try:
        from trading_bot.db.session import init_db, make_session_factory, get_session
        from trading_bot.db.repositories import upsert_trade, upsert_position
        engine = init_db(settings)
        session_factory = make_session_factory(engine)
        session = get_session(session_factory)
        try:
            strategy_tag = _trade_strategy_tag(signal, details)
            upsert_trade(
                session=session,
                ticker=fill.ticker.upper(),
                side="BUY",
                order_type=fill.order_type if hasattr(fill, "order_type") else "market",
                quantity=fill.quantity,
                entry_price=fill.fill_price,
                stop_loss=signal.stop_loss if signal else None,
                profit_target=signal.profit_target if signal else None,
                fees=fill.fees,
                strategy_tag=strategy_tag,
                signal_quality=(str(details.get("quality", "")) if isinstance(details, dict) else None) or None,
                market_regime=(str(details.get("mtf_regime", details.get("v3_regime", ""))) if isinstance(details, dict) else None) or None,
                supermodel_decision=(str(details.get("supermodel_decision", "")) if isinstance(details, dict) else None) or None,
                consensus=(str(details.get("consensus", "")) if isinstance(details, dict) else None) or None,
                entry_volume_ratio=_finite_float(details.get("entry_volume_ratio")) if isinstance(details, dict) else None,
                entry_range_ratio=_finite_float(details.get("entry_range_ratio")) if isinstance(details, dict) else None,
                adaptive_rr=_finite_float(details.get("adaptive_rr")) if isinstance(details, dict) else None,
                status="FILLED",
            )
            upsert_position(
                session=session,
                ticker=fill.ticker.upper(),
                quantity=fill.quantity,
                average_cost=fill.fill_price,
                stop_loss=signal.stop_loss if signal else None,
                profit_target=signal.profit_target if signal else None,
                strategy_tag=strategy_tag,
            )
        finally:
            session.close()
            engine.dispose()
    except Exception:
        logger.debug("Failed to persist trade to database")


def _trade_strategy_tag(signal, details: dict | None = None) -> str | None:
    base = getattr(signal, "strategy_tag", None) if signal else None
    decision = details.get("supermodel_decision") if isinstance(details, dict) else None
    consensus = details.get("consensus") if isinstance(details, dict) else None
    suffixes = []
    if decision:
        stack_suffix = f"stack:{_tag_token(decision, 16)}"
        suffixes.append(stack_suffix)
    else:
        stack_suffix = ""
    if consensus:
        consensus_suffix = f"consensus:{_tag_token(consensus, 8)}"
    else:
        consensus_suffix = ""
    if consensus_suffix:
        suffixes.append(consensus_suffix)
    if not suffixes:
        return base
    suffix = "|".join(suffixes)
    base = str(base or "")[:max(0, 200 - len(suffix))]
    return f"{base}|{suffix}" if base else suffix


def _tag_token(value: object, max_length: int) -> str:
    token = "".join(
        char if char.isalnum() or char in ("_", "-") else "_"
        for char in str(value).lower()
    )
    return token[:max_length].strip("_") or "unknown"


def _paper_evidence_fields(details: dict | None) -> dict[str, object]:
    if not isinstance(details, dict):
        return {}
    compact = {
        key: details[key]
        for key in (
            "signal_mode",
            "consensus",
            "consensus_count",
            "consensus_votes",
            "source_votes",
            "position_size_capped",
            "supermodel_decision",
            "supermodel_score",
            "mtf_aligned",
            "mtf_required",
            "mtf_passed",
            "mtf_regime",
            "entry_timing_passed",
            "entry_volume_ratio",
            "entry_range_ratio",
            "adaptive_rr",
        )
        if key in details
    }
    compact.update({key: value for key, value in details.items() if str(key).startswith("vote_")})
    return compact


def _persist_rl_prediction_to_db(
    symbol: str,
    action: int,
    confidence: float,
    settings: Settings,
) -> None:
    try:
        from trading_bot.db.session import init_db, make_session_factory, get_session
        from trading_bot.db.repositories import upsert_prediction
        engine = init_db(settings)
        session_factory = make_session_factory(engine)
        session = get_session(session_factory)
        try:
            upsert_prediction(
                session=session,
                ticker=symbol.upper(),
                action=int(action),
                confidence=float(confidence),
                model_path=str(settings.rl.model_path),
            )
        finally:
            session.close()
            engine.dispose()
    except Exception:
        logger.debug("Failed to persist RL prediction to database")

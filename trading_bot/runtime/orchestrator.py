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
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.models.signal import TradeSignal
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.portfolio.performance import compute_portfolio_heat, compute_unrealized_pnl
from trading_bot.runtime.decision_log import append_decision_event
from trading_bot.runtime.snapshots import write_snapshot
from trading_bot.risk.risk_manager import evaluate_signal
from trading_bot.strategy.intraday_signal_engine import generate_recent_signal_with_reason
from trading_bot.strategy.supermodel import build_stacked_signal
from trading_bot.rl.utils import rl_model_meta_path, rl_model_symbols

logger = logging.getLogger(__name__)


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
    approved_results: list[tuple[float, str]] = []
    other_results: list[str] = []
    candidate_rows: list[dict[str, object]] = []
    open_tickers = set(state.positions)
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

    # Phase 1: Swarm analysis (read-only overlay)
    swarm_results: dict[str, Any] = {}
    if settings.swarm.enabled:
        try:
            swarm_results = _run_swarm_overlay(symbols, settings)
        except Exception as e:
            logger.warning("Swarm overlay failed: %s", e)
            swarm_results = {}

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
                if settings.swarm.enabled and symbol in swarm_results:
                    swarm_decision = swarm_results[symbol]
                    _augment_details_with_swarm(details, swarm_decision)
                details.update(build_stacked_signal(symbol, signal, details).to_details())
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
                if settings.swarm.enabled and symbol in swarm_results:
                    swarm_decision = swarm_results[symbol]
                    _attach_swarm_row_fields(row, swarm_decision)
                if include_details:
                    row["details"] = details
                candidate_rows.append(row)
                continue

            if settings.swarm.enabled and symbol in swarm_results:
                swarm_decision = swarm_results[symbol]
                _augment_details_with_swarm(details, swarm_decision)
            details.update(build_stacked_signal(symbol, signal, details).to_details())
            detail_text = _format_scan_details(details) if include_details else ""

            # V2.5: Fetch ATR for volatility-adjusted sizing
            atr = _fetch_atr(symbol, settings) if settings.risk.use_atr_sizing else None

            decision = evaluate_signal(
                signal=signal,
                account_equity=state.equity,
                open_tickers=open_tickers,
                portfolio_heat_pct=portfolio_heat,
                atr=atr,
                risk_settings=settings.risk,
                counter_thesis=counter_result,
            )
            if not decision.approved:
                append_decision_event(
                    log_path,
                    {"command": "scan", "ticker": symbol, "status": "REJECTED", "reason": decision.reason},
                )
                other_results.append(f"{symbol} REJECTED {decision.reason}{detail_text}")
                row = {
                    "ticker": symbol,
                    "status": "REJECTED",
                    "reason": decision.reason,
                }
                _attach_supermodel_row_fields(row, details)
                if settings.swarm.enabled and symbol in swarm_results:
                    swarm_decision = swarm_results[symbol]
                    _attach_swarm_row_fields(row, swarm_decision)
                if include_details:
                    row["details"] = details
                candidate_rows.append(row)
                continue

            open_tickers.add(symbol)
            market_status = _market_data_status(
                signal.timestamp, settings.market_data.intraday_interval,
                max_age_minutes=settings.market_data.max_data_age_minutes,
            )
            if market_status == "stale":
                append_decision_event(
                    log_path,
                    {
                        "command": "scan",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": "stale market data",
                        **_paper_evidence_fields(details),
                    },
                )
                market_age = _market_data_age(signal.timestamp)
                other_results.append(f"{symbol} REJECTED stale market data age={market_age}")
                row = {
                    "ticker": symbol,
                    "status": "REJECTED",
                    "reason": "stale market data",
                }
                _attach_supermodel_row_fields(row, details)
                if include_details:
                    row["details"] = details
                candidate_rows.append(row)
                continue

            market_age = _market_data_age(signal.timestamp)
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
            approved_results.append(
                (
                    signal.confidence,
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
            if settings.swarm.enabled and symbol in swarm_results:
                swarm_decision = swarm_results[symbol]
                _attach_swarm_row_fields(row, swarm_decision)
            if include_details:
                row["details"] = details
            candidate_rows.append(row)
        except Exception as exc:
            error_evidence: dict[str, object] = {}
            if settings.swarm.enabled and symbol in swarm_results:
                swarm_decision = swarm_results[symbol]
                _attach_swarm_row_fields(error_evidence, swarm_decision)
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

    approved_results.sort(key=lambda item: item[0], reverse=True)
    candidate_rows.sort(
        key=lambda row: float(row.get("confidence", -1.0)),
        reverse=True,
    )
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
    if settings.swarm.enabled:
        swarm_decisions = [row.get("swarm_decision") for row in candidate_rows if "swarm_decision" in row]
        summary.update({
            "swarm_enabled": True,
            "swarm_approved": sum(1 for d in swarm_decisions if d == "APPROVE"),
            "swarm_rejected": sum(1 for d in swarm_decisions if d == "REJECT"),
            "swarm_hold": sum(1 for d in swarm_decisions if d in ("HOLD", "HOLD_FOR_MORE_INFO")),
        })
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


def _augment_details_with_swarm(details: dict[str, object], swarm_decision: Any) -> None:
    details["swarm_decision"] = swarm_decision.decision
    details["swarm_confidence"] = round(swarm_decision.confidence, 2)
    if swarm_decision.key_rationale:
        details["swarm_rationale"] = swarm_decision.key_rationale
    handoff = _swarm_handoff(swarm_decision)
    if handoff:
        details["swarm_handoff"] = handoff


def _attach_swarm_row_fields(row: dict[str, object], swarm_decision: Any) -> None:
    row["swarm_decision"] = swarm_decision.decision
    row["swarm_confidence"] = round(swarm_decision.confidence, 2)
    if swarm_decision.key_rationale:
        row["swarm_rationale"] = swarm_decision.key_rationale
    handoff = _swarm_handoff(swarm_decision)
    if handoff:
        row["swarm_handoff"] = handoff


def _swarm_handoff(swarm_decision: Any) -> str | None:
    for risk in getattr(swarm_decision, "risk_factors", []) or []:
        text = str(risk)
        if text.startswith("risk_manager handoff:"):
            return text
    return None


def _run_swarm_overlay(symbols: list[str], settings: Settings) -> dict[str, Any]:
    """Run swarm analysis as read-only overlay (Phase 1).

    Returns a dict mapping ticker -> CommitteeDecision for each symbol.
    Does NOT affect trading behavior - results are logged alongside
    scanner output for comparison.
    """
    try:
        from trading_bot.swarm.engine import SwarmEngine
        from trading_bot.swarm.workers import WORKER_CLASSES

        engine = SwarmEngine(
            preset_name=settings.swarm.preset,
            max_concurrent=settings.swarm.max_workers,
        )
        engine.setup_workers(WORKER_CLASSES)

        # Fetch market data for swarm
        frames: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                frame = market_data.fetch_bars(
                    symbol=symbol,
                    period=settings.market_data.daily_period,
                    interval="1d",
                    settings=settings.market_data,
                )
                if frame is not None and not frame.empty:
                    frames[symbol] = frame
            except Exception:
                continue

        if not frames:
            return {}

        # Run swarm analysis
        run_summary = engine.run(
            symbols=list(frames.keys()),
            market_data=frames,
        )

        # Extract per-ticker decisions
        results = {}
        for ticker, decision in run_summary.decisions.items():
            results[ticker] = decision

        return results

    except Exception as e:
        logger.warning("Swarm overlay failed: %s", e)
        return {}


def run_paper_trade(symbols: list[str], settings: Settings, dry_run: bool = False) -> list[str]:
    ledger = PortfolioLedger(Path(settings.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    broker = PaperBroker(
        starting_cash=state.cash,
        fee_per_order=settings.paper.fee_per_order,
        slippage_bps=settings.paper.slippage_bps,
    )
    broker.positions = {
        ticker: position.quantity for ticker, position in state.positions.items()
    }
    results: list[str] = []
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"
    open_tickers = set(state.positions)

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

    swarm_results: dict[str, Any] = {}
    if settings.swarm.enabled:
        try:
            swarm_results = _run_swarm_overlay(symbols, settings)
        except Exception as e:
            logger.warning("Swarm overlay failed: %s", e)
            swarm_results = {}

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
                if symbol in swarm_results:
                    swarm_decision = swarm_results[symbol]
                    _augment_details_with_swarm(details, swarm_decision)
                details.update(build_stacked_signal(symbol, signal, details).to_details())
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
            if symbol in swarm_results:
                swarm_decision = swarm_results[symbol]
                _augment_details_with_swarm(details, swarm_decision)
            details.update(build_stacked_signal(symbol, signal, details).to_details())

            if _market_data_status(signal.timestamp, settings.market_data.intraday_interval,
                                  max_age_minutes=settings.market_data.max_data_age_minutes) == "stale":
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": "stale market data",
                    },
                )
                results.append(f"{symbol} REJECTED stale market data")
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
                alloc = _alloc_mult(Path(settings.app.log_dir), strategy_tag)
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

            decision = evaluate_signal(
                signal=signal,
                account_equity=state.equity,
                open_tickers=open_tickers,
                portfolio_heat_pct=portfolio_heat,
                atr=atr,
                risk_settings=settings.risk,
                counter_thesis=counter_result,
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

            if alloc < 1.0:
                decision.position_size = max(1, int(decision.position_size * alloc))

            if details.get("yellow_mean_reversion"):
                decision.position_size = max(1, int(decision.position_size * settings.risk.yellow_allocation_pct))

            estimated_fill_price = apply_slippage(
                signal.entry_price,
                broker.slippage_bps,
                "BUY",
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
                risk_settings=settings.risk,
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

            ledger.record_fill(fill, side="BUY")
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


def _build_signal_result(symbol: str, settings: Settings) -> tuple[TradeSignal | None, str, dict]:
    # RL path: use RL model if enabled AND trained for this symbol.
    # If RL is enabled but the symbol isn't trained, fall through to V3.
    if getattr(settings, "rl", None) is not None and settings.rl.enabled:
        result = _build_rl_signal_result(symbol, settings)
        if result[0] is not None or "rl_trained_symbols" not in result[2]:
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
    return signal, reason, details


def _build_rl_signal_result(symbol: str, settings: Settings) -> tuple[TradeSignal | None, str, dict]:
    """RL-based signal generation using trained DRL agent.

    Loads a trained model and uses it to predict actions (HOLD/BUY/SELL).
    Converts BUY actions to TradeSignal objects with risk management parameters.
    """
    from trading_bot.rl.agent import RLAgent

    model_path = Path(settings.app.log_dir).parent / settings.rl.model_path
    if not model_path.exists():
        return None, f"RL model not found: {model_path}", {}
    trained_symbols = rl_model_symbols(model_path)
    if not trained_symbols:
        return None, f"RL model metadata missing or empty: {rl_model_meta_path(model_path)}", {}

    try:
        agent = RLAgent.load(
            model_path=model_path,
        )
    except Exception as e:
        return None, f"RL model load failed: {e}", {}

    symbol_upper = symbol.upper().strip()
    is_trained_symbol = symbol_upper in trained_symbols
    
    daily_frames = {}
    intraday_frames = {}
    
    symbols_for_inference = trained_symbols.copy()
    if not is_trained_symbol:
        symbols_for_inference.append(symbol_upper)
    
    for trained_symbol in symbols_for_inference:
        daily_frame, daily_valid = market_data.fetch_and_validate_bars(
            trained_symbol,
            settings.market_data.daily_period,
            "1d",
            settings.market_data,
        )
        if not daily_valid.valid:
            return None, f"{trained_symbol} daily data validation failed: {daily_valid.reason}", {}

        intraday_frame, intraday_valid = market_data.fetch_and_validate_bars(
            trained_symbol,
            settings.market_data.intraday_period,
            settings.market_data.intraday_interval,
            settings.market_data,
        )
        if not intraday_valid.valid:
            return None, f"{trained_symbol} intraday data validation failed: {intraday_valid.reason}", {}
        daily_frames[trained_symbol] = daily_frame
        intraday_frames[trained_symbol] = intraday_frame

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

    current_price = float(intraday_frame["close"].iloc[-1])
    action, confidence = agent.predict_signal(
        daily_frame=daily_frame,
        ticker=symbol_upper,
        portfolio_weight=0.0,
        unrealized_pnl_pct=0.0,
        cash_ratio=1.0,
        symbols=symbols_for_inference,
        market_frames=daily_frames,
    )

    _persist_rl_prediction_to_db(symbol, action, confidence, settings)

    details = _scan_details(daily_frame, intraday_frame)
    details["rl_action"] = action
    details["rl_confidence"] = confidence
    details["rl_agent_type"] = settings.rl.agent_type
    details["rl_trained_symbols"] = trained_symbols
    details["rl_untrained_symbol"] = not is_trained_symbol

    if action == 0:
        return None, f"RL agent predicts HOLD (confidence={confidence:.2f})", details

    if action == 2:
        return None, f"RL agent predicts SELL (confidence={confidence:.2f})", details

    confidence_threshold = settings.rl.action_confidence_threshold
    if not is_trained_symbol:
        confidence_threshold = confidence_threshold * 0.8
        confidence *= 0.85

    if confidence < confidence_threshold:
        return None, f"RL confidence {confidence:.2f} below threshold {confidence_threshold}", details

    atr = float(intraday_frame[f"atr_{settings.risk.atr_period}"].iloc[-1]) if f"atr_{settings.risk.atr_period}" in intraday_frame.columns else current_price * 0.02
    stop_distance = atr * settings.risk.atr_multiplier
    stop_loss = current_price - stop_distance
    profit_target = current_price + (stop_distance * 2.0)
    risk_reward_ratio = (profit_target - current_price) / (current_price - stop_loss) if current_price > stop_loss else 0.0

    signal = TradeSignal(
        ticker=symbol,
        timeframe="intraday",
        action="BUY",
        entry_price=current_price,
        stop_loss=stop_loss,
        profit_target=profit_target,
        risk_reward_ratio=risk_reward_ratio,
        confidence=confidence,
        reasons=[f"RL {settings.rl.agent_type} signal", f"confidence={confidence:.2f}"],
        strategy_tag=f"rl_{settings.rl.agent_type}",
        timestamp=datetime.now(timezone.utc),
    )

    return signal, f"RL {settings.rl.agent_type} approved", details


def _build_v3_signal_result(symbol: str, settings: Settings):
    """V3 strategy path: regime detection + 5-factor confluence scoring.

    Produces a :class:`TradeSignal` adapted from a
    :class:`StrategySelection`. Falls back to the legacy path's details
    structure so downstream scan/report code is unchanged.
    """
    from trading_bot.strategy.strategy_selector import StrategySelector

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
    daily_frame = add_ema(daily_frame, period=20, column_name="ema_20")
    daily_frame = add_sma(daily_frame, period=50, column_name="sma_50")
    daily_frame = add_atr(daily_frame, period=settings.risk.atr_period)
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
    return signal, "v3 approved", details


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

    if details.get("swarm_decision"):
        parts.append(
            f"swarm={details.get('swarm_decision')}"
            f":{details.get('swarm_confidence')}"
        )
    if details.get("swarm_handoff"):
        parts.append(f"swarm_handoff={str(details.get('swarm_handoff')).replace(' ', '_')}")

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


def _market_data_status(signal_timestamp: datetime, interval: str, max_age_minutes: int = 30) -> str:
    signal_timestamp = _ensure_aware(signal_timestamp)
    age = _scan_now(signal_timestamp) - signal_timestamp
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
        from trading_bot.db.repositories import upsert_scan_result
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
                    upsert_scan_result(
                        session=session,
                        ticker=ticker.upper(),
                        action=action,
                        confidence=confidence,
                        score=score,
                        strategy_tag=strategy_tag,
                        reasons=reasons,
                        details=details_dict,
                    )
                elif status == "NO_SIGNAL":
                    reason = row.get("reason", "no signal")
                    upsert_scan_result(
                        session=session,
                        ticker=ticker.upper(),
                        action="HOLD",
                        confidence=0.0,
                        reasons=[str(reason)],
                        details=_scan_row_details_for_persistence(row),
                    )
                elif status == "REJECTED":
                    reason = row.get("reason", "rejected")
                    upsert_scan_result(
                        session=session,
                        ticker=ticker.upper(),
                        action="HOLD",
                        confidence=0.0,
                        reasons=[str(reason)],
                        details=_scan_row_details_for_persistence(row),
                    )
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


def _scan_row_details_for_persistence(row: dict) -> dict | None:
    details = row.get("details")
    if isinstance(details, dict):
        return details
    compact = {
        key: row[key]
        for key in (
            "supermodel_decision",
            "supermodel_score",
            "swarm_decision",
            "swarm_confidence",
            "swarm_rationale",
            "swarm_handoff",
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
    swarm_decision = details.get("swarm_decision") if isinstance(details, dict) else None
    suffixes = []
    if decision:
        suffixes.append(f"stack:{_tag_token(decision, 16)}")
    if swarm_decision:
        suffixes.append(f"swarm:{_short_swarm_decision(swarm_decision)}")
    if not suffixes:
        return base
    suffix = "|".join(suffixes)[:50]
    base = str(base or "")[:max(0, 49 - len(suffix))]
    return f"{base}|{suffix}" if base else suffix


def _short_swarm_decision(decision: object) -> str:
    value = _tag_token(decision, 20)
    if value == "hold_for_more_info":
        return "hold"
    return value


def _tag_token(value: object, max_length: int) -> str:
    token = "".join(
        char if char.isalnum() or char in ("_", "-") else "_"
        for char in str(value).lower()
    )
    return token[:max_length].strip("_") or "unknown"


def _paper_evidence_fields(details: dict | None) -> dict[str, object]:
    if not isinstance(details, dict):
        return {}
    return {
        key: details[key]
        for key in (
            "supermodel_decision",
            "supermodel_score",
            "swarm_decision",
            "swarm_confidence",
            "swarm_handoff",
        )
        if key in details
    }


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

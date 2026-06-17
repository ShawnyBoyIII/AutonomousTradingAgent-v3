from __future__ import annotations

from datetime import datetime, timedelta
import math
from pathlib import Path

from trading_bot.config.settings import Settings
from trading_bot.data import market_data
from trading_bot.data.indicators import add_ema, add_sma
from trading_bot.execution.order_manager import submit_signal_as_order
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.runtime.decision_log import append_decision_event
from trading_bot.runtime.snapshots import write_snapshot
from trading_bot.risk.risk_manager import evaluate_signal
from trading_bot.strategy.intraday_signal_engine import generate_recent_signal_with_reason


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

    for symbol in (value.strip() for value in symbols if value.strip()):
        try:
            signal, no_signal_reason, details = _build_signal_result(symbol, settings)
            detail_text = _format_scan_details(details) if include_details else ""
            if signal is None:
                append_decision_event(
                    log_path,
                    {
                        "command": "scan",
                        "ticker": symbol,
                        "status": "NO_SIGNAL",
                        "reason": no_signal_reason,
                    },
                )
                other_results.append(f"{symbol} NO_SIGNAL reason={no_signal_reason}{detail_text}")
                row = {"ticker": symbol, "status": "NO_SIGNAL", "reason": no_signal_reason}
                if include_details:
                    row["details"] = details
                candidate_rows.append(row)
                continue

            decision = evaluate_signal(
                signal=signal,
                account_equity=state.equity,
                open_tickers=open_tickers,
                risk_settings=settings.risk,
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
                if include_details:
                    row["details"] = details
                candidate_rows.append(row)
                continue

            open_tickers.add(symbol)
            market_status = _market_data_status(
                signal.timestamp, settings.market_data.intraday_interval
            )
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
            if include_details:
                row["details"] = details
            candidate_rows.append(row)
        except Exception as exc:
            append_decision_event(
                log_path,
                {"command": "scan", "ticker": symbol, "status": "ERROR", "error": str(exc)},
            )
            other_results.append(f"{symbol} ERROR {exc}")
            candidate_rows.append({"ticker": symbol, "status": "ERROR", "error": str(exc)})

    approved_results.sort(key=lambda item: item[0], reverse=True)
    candidate_rows.sort(
        key=lambda row: float(row.get("confidence", -1.0)),
        reverse=True,
    )
    lines = [value for _, value in approved_results] + other_results
    write_snapshot(
        settings.app.scan_results_path,
        {
            "mode": "scan",
            "summary": {
                "symbols": len([value for value in symbols if value.strip()]),
                "approved": sum(1 for row in candidate_rows if row["status"] == "APPROVED"),
                "rejected": sum(1 for row in candidate_rows if row["status"] == "REJECTED"),
                "no_signal": sum(1 for row in candidate_rows if row["status"] == "NO_SIGNAL"),
                "errors": sum(1 for row in candidate_rows if row["status"] == "ERROR"),
            },
            "candidates": candidate_rows,
        },
    )
    return {"lines": lines, "candidates": candidate_rows}


def run_paper_trade(symbols: list[str], settings: Settings) -> list[str]:
    ledger = PortfolioLedger(Path(settings.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    broker = PaperBroker(starting_cash=state.cash, fee_per_order=1.0, slippage_bps=0)
    broker.positions = {
        ticker: position.quantity for ticker, position in state.positions.items()
    }
    results: list[str] = []
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"
    open_tickers = set(state.positions)

    for symbol in (value.strip() for value in symbols if value.strip()):
        try:
            signal = _build_signal(symbol, settings)
            if signal is None:
                append_decision_event(
                    log_path,
                    {"command": "paper-trade", "ticker": symbol, "status": "NO_SIGNAL"},
                )
                results.append(f"{symbol} NO_SIGNAL")
                continue

            decision = evaluate_signal(
                signal=signal,
                account_equity=state.equity,
                open_tickers=open_tickers,
                risk_settings=settings.risk,
            )
            if not decision.approved:
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": decision.reason,
                    },
                )
                results.append(f"{symbol} REJECTED {decision.reason}")
                continue

            estimated_total_cost = (signal.entry_price * decision.position_size) + broker.fee_per_order
            if broker.cash < estimated_total_cost:
                append_decision_event(
                    log_path,
                    {
                        "command": "paper-trade",
                        "ticker": symbol,
                        "status": "REJECTED",
                        "reason": "insufficient cash",
                    },
                )
                results.append(f"{symbol} REJECTED insufficient cash")
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
                    },
                )
                results.append(f"{symbol} REJECTED broker rejected order")
                continue

            ledger.record_fill(fill, side="BUY")
            updated_state = _portfolio_state_from_broker(
                broker,
                signal,
                previous_state=state,
                fill_fees=fill.fees,
            )
            ledger.save_portfolio_state(updated_state)
            state = updated_state
            open_tickers.add(symbol)
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


def _build_signal(symbol: str, settings: Settings):
    signal, _ = _build_signal_with_reason(symbol, settings)
    return signal


def _build_signal_with_reason(symbol: str, settings: Settings):
    signal, reason, _ = _build_signal_result(symbol, settings)
    return signal, reason


def _build_signal_result(symbol: str, settings: Settings):
    daily_frame = market_data.fetch_bars(
        symbol,
        settings.market_data.daily_period,
        "1d",
    )
    intraday_frame = market_data.fetch_bars(
        symbol,
        settings.market_data.intraday_period,
        settings.market_data.intraday_interval,
    )
    daily_frame = add_ema(daily_frame, period=20, column_name="ema_20")
    daily_frame = add_sma(daily_frame, period=50, column_name="sma_50")
    intraday_frame = _drop_trailing_zero_volume_bars(intraday_frame)
    intraday_frame["volume_avg_5"] = intraday_frame["volume"].rolling(5).mean()
    signal, reason = generate_recent_signal_with_reason(symbol, daily_frame, intraday_frame)
    detail_frame = _frame_through_timestamp(intraday_frame, signal.timestamp) if signal else intraday_frame
    return signal, reason, _scan_details(daily_frame, detail_frame)


def _drop_trailing_zero_volume_bars(frame):
    if frame.empty or "volume" not in frame.columns:
        return frame.copy(deep=True)

    end = len(frame)
    while end > 0:
        volume = _finite_float(frame.iloc[end - 1].get("volume"))
        if volume is not None and volume > 0:
            break
        end -= 1
    return frame.iloc[:end].copy(deep=True)


def _frame_through_timestamp(frame, timestamp: datetime):
    if frame.empty or "timestamp" not in frame.columns:
        return frame
    matches = frame.index[frame["timestamp"] == timestamp].tolist()
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


def _format_scan_details(details: dict[str, float | int]) -> str:
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
            parts.append(f"{key}={float(value):.2f}")
    return f" {' '.join(parts)}" if parts else ""


def _scan_quality(details: dict[str, float | int]) -> str:
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


def _portfolio_state_from_broker(
    broker: PaperBroker,
    signal,
    previous_state: PortfolioState,
    fill_fees: float,
) -> PortfolioState:
    positions = {
        ticker: Position(
            ticker=ticker,
            quantity=quantity,
            average_cost=signal.entry_price,
        )
        for ticker, quantity in broker.positions.items()
        if quantity > 0
    }
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


def _market_data_status(signal_timestamp: datetime, interval: str) -> str:
    age = _scan_now(signal_timestamp) - signal_timestamp
    return "stale" if age > _stale_after(interval) else "fresh"


def _market_data_age(signal_timestamp: datetime) -> str:
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


def _scan_now(signal_timestamp: datetime) -> datetime:
    return datetime.now(tz=signal_timestamp.tzinfo)

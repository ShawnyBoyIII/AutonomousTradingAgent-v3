from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
import logging

import pandas as pd

from trading_bot.config.settings import Settings
from trading_bot.data import market_data
from trading_bot.data.indicators import add_atr, add_rsi
from trading_bot.events.types import MarketBarEvent
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.portfolio.performance import compute_portfolio_heat
from trading_bot.runtime.decision_log import append_decision_event
from trading_bot.runtime.orchestrator import (
    run_scan,
    run_paper_trade,
)
from trading_bot.runtime.position_exit import fill_partial_take_profit_position, fill_sell_position
from trading_bot.learning.experiments.runtime_canary import load_runtime_canary
from trading_bot.runtime.position_management import evaluate_exit_priority
from trading_bot.scout import build_scout_candidates

logger = logging.getLogger(__name__)


class LoopStats:
    """Tracks statistics across loop iterations."""

    def __init__(self) -> None:
        self.cycle: int = 0
        self.total_scans: int = 0
        self.total_trades: int = 0
        self.total_exits: int = 0
        self.total_rejections: int = 0
        self.total_errors: int = 0
        self.start_time: float = 0.0
        self.last_cycle_time: float = 0.0
        self.consecutive_failures: int = 0
        self.max_consecutive_failures: int = 0

    def reset_cycle(self) -> None:
        self.cycle += 1
        self.total_scans += 1

    def log_trades(self, trade_count: int) -> None:
        self.total_trades += trade_count

    def log_exits(self, exit_count: int) -> None:
        self.total_exits += exit_count

    def log_rejections(self, reject_count: int) -> None:
        self.total_rejections += reject_count

    def log_errors(self) -> None:
        self.total_errors += 1
        self.consecutive_failures += 1
        if self.consecutive_failures > self.max_consecutive_failures:
            self.max_consecutive_failures = self.consecutive_failures

    def reset_failures(self) -> None:
        self.consecutive_failures = 0

    def cycle_duration(self) -> float:
        if self.last_cycle_time > 0:
            return self.last_cycle_time
        return time.monotonic() - self.start_time

    def uptime(self) -> float:
        return time.monotonic() - self.start_time

    def summary(self) -> dict:
        return {
            "cycle": self.cycle,
            "total_scans": self.total_scans,
            "total_trades": self.total_trades,
            "total_exits": self.total_exits,
            "total_rejections": self.total_rejections,
            "total_errors": self.total_errors,
            "uptime_seconds": round(self.uptime(), 1),
            "avg_cycle_seconds": round(self.uptime() / max(self.cycle, 1), 1),
            "max_consecutive_failures": self.max_consecutive_failures,
        }


def _build_universe(settings: Settings) -> list[str]:
    """Build universe from scout candidates."""
    from trading_bot.runtime.snapshots import write_snapshot

    fetch_limit = max(settings.scout.max_universe_size, settings.scout.max_snapshot_candidates)
    rows = market_data.fetch_small_cap_candidates(
        limit=fetch_limit,
        screeners=settings.scout.screeners,
    )
    scout_result = build_scout_candidates(rows, settings.scout)
    included_symbols = scout_result.included_symbols

    path = Path(settings.app.universe_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text("".join(f"{symbol}\n" for symbol in included_symbols), encoding="utf-8")
    tmp_path.replace(path)

    snapshot_limit = max(settings.scout.max_universe_size, settings.scout.max_snapshot_candidates)
    scout_dump = scout_result.model_dump()
    write_snapshot(
        settings.app.universe_candidates_path,
        {
            "mode": "universe",
            "summary": scout_dump["summary"],
            "candidates": scout_dump["candidates"][:snapshot_limit],
        },
    )

    return included_symbols


def _read_universe_symbols(settings: Settings) -> list[str]:
    """Read symbols from universe file or candidates snapshot."""
    universe_path = Path(settings.app.universe_path)
    if universe_path.exists():
        values: list[str] = []
        for raw_line in universe_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            values.extend(symbol.strip() for symbol in line.split(",") if symbol.strip())
        if values:
            return values

    candidates_path = Path(settings.app.universe_candidates_path)
    if candidates_path.exists():
        import json
        from trading_bot.models.scout import UniverseCandidatesSnapshot

        snapshot = json.loads(candidates_path.read_text(encoding="utf-8"))
        parsed = UniverseCandidatesSnapshot.model_validate(snapshot)
        ranked = [
            candidate
            for candidate in parsed.candidates
            if candidate.included and candidate.ticker.strip()
        ]
        if ranked:
            ranked.sort(key=lambda candidate: candidate.rank or 999999)
            return [candidate.ticker.strip() for candidate in ranked]

    return []


def _run_manage_positions_once(
    settings: Settings,
    ledger: PortfolioLedger,
    *,
    runtime_canary: Any = None,
) -> dict:
    """Run one position-management check (EOD, stop, target, trail).

    Mirrors _run_manage_positions_once from cli/app.py but as a reusable function.
    """
    from trading_bot.execution.paper_broker import PaperBroker
    from trading_bot.models.portfolio import Position
    from trading_bot.runtime.orchestrator import (
        _calculate_portfolio_heat,
        _fetch_atr,
        _build_signal_result,
        _evaluate_counter_thesis_for_signal,
        _scan_quality,
    )
    from trading_bot.runtime.snapshots import write_snapshot
    from trading_bot.safety.kill_switch import check_kill_switch_before_trade
    from trading_bot.safety.circuit_breaker import check_circuit_breakers
    from trading_bot.risk.risk_manager import evaluate_signal
    from trading_bot.strategy.trailing_stop import next_trailing_stop

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

    portfolio_heat = _calculate_portfolio_heat(state, settings)

    allowed, reason = check_kill_switch_before_trade(ledger)
    if not allowed:
        return {"positions": len(state.positions), "actions": 0, "lines": [f"KILL_SWITCH: {reason}"], "exit_events": []}

    cb_allowed, cb_reason = check_circuit_breakers(ledger, settings)
    if not cb_allowed:
        log_path = Path(settings.app.log_dir) / "decision-log.jsonl"
        append_decision_event(log_path, {"command": "manage-positions", "status": "CIRCUIT_BREAKER", "reason": cb_reason})
        return {"positions": len(state.positions), "actions": 0, "lines": [f"CIRCUIT_BREAKER: {cb_reason}"], "exit_events": []}

    lines: list[str] = []
    exit_events: list[dict] = []
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"
    now = datetime.now(timezone.utc)

    # Idempotency guard: skip exits for tickers recently sold by a concurrent process
    _EXIT_COOLDOWN_SECONDS = 120

    def _recently_exited(ticker: str) -> bool:
        ts = state.last_exited_at.get(ticker)
        if not ts:
            return False
        try:
            exited_at = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return False
        if exited_at.tzinfo is None:
            exited_at = exited_at.replace(tzinfo=timezone.utc)
        return (now - exited_at).total_seconds() < _EXIT_COOLDOWN_SECONDS

    for ticker, position in list(state.positions.items()):
        if position.quantity <= 0:
            continue

        try:
            intraday_frame, intraday_valid = market_data.fetch_and_validate_bars(
                ticker,
                settings.market_data.intraday_period,
                settings.market_data.intraday_interval,
                settings.market_data,
            )
            if not intraday_valid.valid:
                lines.append(f"{ticker} SKIP validation_failed={intraday_valid.reason}")
                continue

            if intraday_frame.empty:
                lines.append(f"{ticker} SKIP no_intraday_data")
                continue

            from trading_bot.runtime.latency import data_age_minutes, frame_last_timestamp

            latest_bar = intraday_frame.iloc[-1]
            market_age = data_age_minutes(frame_last_timestamp(intraday_frame), now)
            if market_age is not None and market_age > 5:
                lines.append(f"{ticker} SKIP stale_data age={int(market_age * 60)}s")
                continue

            current_price = float(latest_bar["close"])
            line_parts = [f"{ticker} price={current_price:.2f} qty={position.quantity}"]

            # Idempotency: skip if another process already sold this ticker
            if _recently_exited(ticker):
                lines.append(f"{ticker} SKIP recently-exited-cooldown")
                continue

            # Enforce min_stop_distance_pct on existing positions
            min_stop_pct = settings.risk.min_stop_distance_pct
            if min_stop_pct > 0 and position.stop_loss is not None:
                entry = position.average_cost
                min_stop = round(entry * (1.0 - min_stop_pct / 100.0), 4)
                # Only widen stops that are still protective (below entry); a
                # stop already ratcheted up by trailing should not be undone.
                if position.stop_loss > min_stop and position.stop_loss < entry:
                    old_stop = position.stop_loss
                    pos = position.model_copy(update={"stop_loss": min_stop})
                    state.positions[ticker] = pos
                    position = pos
                    line_parts.append(f"stop_widened {old_stop:.4f}->{min_stop:.4f}")

            def _counter_thesis_check():
                if (
                    getattr(settings, "counter_thesis", None) is None
                    or not settings.counter_thesis.enabled
                ):
                    return None
                signal, _, details = _build_signal_result(ticker, settings)
                if signal is not None:
                    counter_result = _evaluate_counter_thesis_for_signal(ticker, signal, settings)
                    if counter_result is not None and counter_result.exit_triggered:
                        return counter_result
                return None

            def _trailing_stop_check():
                atr = _fetch_atr(ticker, settings) if settings.risk.use_atr_sizing else None
                live_position = state.positions[ticker]
                trailing_stop, method = next_trailing_stop(live_position, current_price, atr)
                # Ratchet first: write any tighter stop to state BEFORE the
                # exit decision is made. Previously this callable only
                # returned a value when price had dropped below the new
                # stop, so position.stop_loss was never ratcheted in the
                # continuous loop while the CLI did ratchet at the same
                # bar. (Round 1 review fix.)
                if trailing_stop is not None:
                    new_high = live_position.highest_high
                    if new_high is None or current_price > new_high:
                        new_high = current_price
                    if (
                        trailing_stop > (live_position.stop_loss or trailing_stop - 1)
                        or new_high != live_position.highest_high
                    ):
                        state.positions[ticker] = live_position.model_copy(update={
                            "stop_loss": trailing_stop,
                            "highest_high": new_high,
                        })
                    if current_price <= trailing_stop:
                        # Signal exit only after ratchet has been written
                        return trailing_stop, method
                return None

            from zoneinfo import ZoneInfo
            from trading_bot.runtime.session import should_eod_exit

            # Re-read live position from state. The trailing-stop closure
            # above may have written a ratcheted stop_loss / highest_high
            # to state.positions[ticker]; the closure-captured `position`
            # is stale. Critical review finding #1: reading from `position`
            # here silently clobbered the ratchet on the no-exit
            # highest_high cleanup at the end of the loop body.
            live_position = state.positions[ticker]
            decision = evaluate_exit_priority(
                position=live_position,
                current_price=current_price,
                settings=settings,
                now=now,
                eod_active=should_eod_exit(
                    now.astimezone(ZoneInfo(settings.app.timezone)), settings.session
                ),
                counter_thesis_check=_counter_thesis_check,
                trailing_stop_check=_trailing_stop_check,
            )
            if decision.partial:
                state, event, line = fill_partial_take_profit_position(
                    ticker=ticker,
                    position=live_position,
                    submitted_at=now,
                    last_price=current_price,
                    broker=broker,
                    ledger=ledger,
                    state=state,
                    log_path=log_path,
                    fraction=settings.paper.partial_take_profit_fraction,
                    settings=settings,
                    runtime_canary=runtime_canary,
                )
                append_decision_event(log_path, event)
                line_parts.append(line)
                exit_events.append(
                    {
                        "ticker": ticker,
                        "reason": event["reason"],
                        "quantity": event["quantity"],
                        "fill_price": event["fill_price"],
                    }
                )
                continue
            if decision.should_exit:
                updated_state = _close_position(
                    ticker=ticker,
                    position=live_position,
                    broker=broker,
                    ledger=ledger,
                    current_price=current_price,
                    settings=settings,
                    line_parts=line_parts,
                    exit_events=exit_events,
                    log_path=log_path,
                    reason=decision.reason,
                    state=state,
                    bars=intraday_frame,
                    runtime_canary=runtime_canary,
                )
                if updated_state is not None:
                    state = updated_state
                continue

            # No exit triggered - read live position (might have been
            # ratcheted above). The previous implementation read from
            # the closure-captured `position`, which didn't include the
            # ratchet, and used it to model_copy a new position with the
            # OLD stop_loss. That silently undid the trailing-stop
            # ratchet on every bar where current_price > position.highest_high.
            live_position = state.positions[ticker]
            if live_position.highest_high is None or current_price > live_position.highest_high:
                state.positions[ticker] = live_position.model_copy(update={"highest_high": current_price})

            highest_high = state.positions[ticker].highest_high
            high_text = f"{highest_high:.2f}" if highest_high is not None else "N/A"
            line_parts.append(f"highest_high={high_text}")
            lines.append(" ".join(line_parts))

        except Exception as exc:
            logger.error(f"manage_positions error ticker={ticker} error={exc}")
            lines.append(f"{ticker} ERROR {exc}")
            append_decision_event(
                log_path,
                {"command": "manage-positions", "ticker": ticker, "status": "ERROR", "error": str(exc)},
            )

    ledger.save_portfolio_state(state)
    ledger.record_equity_snapshot(state, timestamp=now)
    write_snapshot(
        settings.app.portfolio_summary_path,
        {
            "mode": "manage-positions",
            "positions": len(state.positions),
            "actions": len(exit_events),
            "cash": state.cash,
            "equity": state.equity,
        },
    )

    return {
        "positions": len(state.positions),
        "actions": len(exit_events),
        "lines": lines,
        "exit_events": exit_events,
    }


def _close_position(
    ticker: str,
    position: Position,
    broker: PaperBroker,
    ledger: PortfolioLedger,
    current_price: float,
    settings: Settings,
    line_parts: list[str],
    exit_events: list[dict],
    log_path: Path,
    reason: str,
    state: PortfolioState | None = None,
    bars=None,
    runtime_canary=None,
) -> PortfolioState | None:
    """Close a position and return the updated portfolio state."""
    exit_rsi = None
    exit_atr = None
    hold_duration = None
    exit_strategy = None

    if bars is not None and not bars.empty:
        try:
            bar_copy = bars.copy()
            bar_copy = add_rsi(bar_copy, period=14)
            rsi_val = bar_copy["rsi_14"].iloc[-1] if "rsi_14" in bar_copy.columns else None
            if rsi_val is not None and not (isinstance(rsi_val, float) and pd.isna(rsi_val)):
                exit_rsi = float(rsi_val)
        except (KeyError, ValueError):
            exit_rsi = None
        try:
            bar_copy = bars.copy()
            bar_copy = add_atr(bar_copy, period=14)
            atr_val = bar_copy["atr_14"].iloc[-1] if "atr_14" in bar_copy.columns else None
            if atr_val is not None and not (isinstance(atr_val, float) and pd.isna(atr_val)):
                exit_atr = float(atr_val)
        except (KeyError, ValueError):
            exit_atr = None

    if position.entry_at is not None:
        entry_at = position.entry_at
        if entry_at.tzinfo is None:
            entry_at = entry_at.replace(tzinfo=timezone.utc)
        hold_duration = (datetime.now(timezone.utc) - entry_at).total_seconds() / 60.0

    exit_strategy = getattr(position, "strategy_tag", None)

    try:
        updated_state, event, line = fill_sell_position(
            ticker=ticker,
            position=position,
            reason=reason,
            submitted_at=datetime.now(timezone.utc),
            last_price=current_price,
            broker=broker,
            ledger=ledger,
            state=state or ledger.ensure_portfolio_state(),
            log_path=log_path,
            exit_rsi=exit_rsi,
            exit_atr=exit_atr,
            hold_duration_minutes=hold_duration,
            exit_strategy=exit_strategy,
            exit_reason=reason,
            settings=settings,
            runtime_canary=runtime_canary,
        )
    except ValueError:
        line_parts.append(f"SELL_FAILED reason={reason}")
        return None

    append_decision_event(log_path, event)
    line_parts.append(line)
    exit_events.append(
        {
            "ticker": ticker,
            "reason": reason,
            "quantity": event["quantity"],
            "fill_price": event["fill_price"],
        }
    )
    return updated_state


def run_continuous_loop(
    settings: Settings,
    interval_seconds: int = 300,
    max_cycles: int | None = None,
    build_universe: bool = True,
    dry_run: bool = False,
    max_failures: int = 10,
) -> LoopStats:
    """Run the continuous paper-trading loop.

    Each cycle:
    1. Build universe (optional)
    2. Run scan on watchlist
    3. Run paper-trade on approved signals
    4. Run manage-positions for existing positions
    5. Sleep for interval

    Args:
        settings: Application settings
        interval_seconds: Seconds between cycles
        max_cycles: Maximum number of cycles (None = infinite)
        build_universe: Whether to refresh universe each cycle
        dry_run: Preview trades without executing
        max_failures: Consecutive failures before circuit breaker

    Returns:
        LoopStats with statistics from the run
    """
    stats = LoopStats()
    stats.start_time = time.monotonic()

    ledger = PortfolioLedger(Path(settings.app.state_db_path))
    state = ledger.ensure_portfolio_state()

    logger.info(
        f"continuous_loop_start interval={interval_seconds}s max_cycles={max_cycles} "
        f"build_universe={build_universe} dry_run={dry_run}"
    )

    cycle = 1
    while True:
        if max_cycles is not None and cycle > max_cycles:
            logger.info(f"continuous_loop_max_cycles_reached cycles={cycle}")
            break

        stats.reset_cycle()

        # Load runtime canary once per cycle (Round 1 fix). When no
        # experiment is in CANARY, this returns None and the trading
        # paths behave identically to the no-canary contract.
        runtime_canary = load_runtime_canary(settings, ledger)

        try:
            # Phase 1: Build universe
            symbols: list[str] = []
            if build_universe:
                symbols = _build_universe(settings)
                logger.info(f"universe_built symbols={len(symbols)}")

            if not symbols:
                symbols = _read_universe_symbols(settings)
                logger.info(f"universe_read symbols={len(symbols)}")

            if not symbols:
                logger.warning("no_symbols_in_universe skipping cycle")
                stats.log_rejections(0)
                if interval_seconds > 0:
                    time.sleep(interval_seconds)
                cycle += 1
                continue

            # Phase 2: Scan
            scan_result = run_scan(symbols, settings)
            approved = [
                row for row in scan_result.get("candidates", [])
                if row.get("status") == "APPROVED" and row.get("quality") == "GREEN"
            ]

            stats.log_rejections(
                scan_result.get("summary", {}).get("rejected", 0)
            )

            # Phase 3: Paper trade
            approved_symbols = [c["ticker"] for c in approved]
            if approved_symbols:
                trade_results = run_paper_trade(
                    approved_symbols, settings, dry_run=dry_run, runtime_canary=runtime_canary
                )
                trade_count = sum(1 for r in trade_results if "FILLED" in r or "DRY_RUN" in r)
                stats.log_trades(trade_count)
                logger.info(f"paper_trades executed trades={trade_count} results={len(trade_results)}")
            else:
                logger.info("no_approved_signals skipping_paper_trade")

            # Phase 4: Manage positions
            manage_result = _run_manage_positions_once(
                settings, ledger, runtime_canary=runtime_canary
            )
            stats.log_exits(manage_result.get("actions", 0))
            logger.info(
                f"manage_positions positions={manage_result.get('positions', 0)} "
                f"actions={manage_result.get('actions', 0)}"
            )

            # Reset failure counter on success
            stats.reset_failures()

            # Log cycle summary
            logger.info(
                f"cycle_complete cycle={cycle} scans={stats.total_scans} "
                f"trades={stats.total_trades} exits={stats.total_exits} "
                f"rejections={stats.total_rejections} errors={stats.total_errors}"
            )

        except Exception as exc:
            stats.log_errors()
            logger.error(f"cycle_error cycle={cycle} error={exc}", exc_info=True)

            if stats.consecutive_failures >= max_failures:
                logger.critical(f"circuit_breaker_open consecutive_failures={stats.consecutive_failures}")
                break

            backoff = min(2 ** (stats.consecutive_failures - 1), 60)
            logger.warning(f"backing_off cycle={cycle} failures={stats.consecutive_failures} seconds={backoff}")
            time.sleep(backoff)
            continue

        stats.last_cycle_time = time.monotonic() - stats.start_time

        if max_cycles is not None and cycle >= max_cycles:
            break

        if interval_seconds > 0:
            time.sleep(interval_seconds)

        cycle += 1

    logger.info(f"continuous_loop_stopped stats={stats.summary()}")
    return stats

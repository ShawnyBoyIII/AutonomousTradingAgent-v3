from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time
import logging

from trading_bot.config.settings import Settings
from trading_bot.data import market_data
from trading_bot.events.bus import MessageBus
from trading_bot.events.cache import Cache
from trading_bot.events.loop import EventLoop
from trading_bot.events.orchestrator import create_event_orchestrator
from trading_bot.events.types import (
    MarketBarEvent,
    StrategySignalEvent,
    SystemTickEvent,
)
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.portfolio.performance import compute_portfolio_heat
from trading_bot.runtime.orchestrator import (
    run_scan,
    run_paper_trade,
)
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


def _run_manage_positions_once(settings: Settings, ledger: PortfolioLedger) -> dict:
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
        _market_data_status,
        _market_data_age,
        _scan_quality,
        _portfolio_state_from_broker,
    )
    from trading_bot.runtime.decision_log import append_decision_event
    from trading_bot.runtime.snapshots import write_snapshot
    from trading_bot.safety.kill_switch import check_kill_switch_before_trade
    from trading_bot.safety.circuit_breaker import check_circuit_breakers
    from trading_bot.risk.risk_manager import evaluate_signal
    from trading_bot.strategy.trailing_stop import next_trailing_stop
    from trading_bot.execution.fills import apply_slippage
    from trading_bot.execution.order_manager import submit_signal_as_order

    state = ledger.ensure_portfolio_state()
    broker = PaperBroker(
        starting_cash=state.cash,
        fee_per_order=settings.paper.fee_per_order,
        slippage_bps=settings.paper.slippage_bps,
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
    now = datetime.now()

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

            latest_bar = intraday_frame.iloc[-1]
            market_age = _market_data_age(latest_bar.name if hasattr(latest_bar, 'name') else now)
            if market_age is not None and market_age > 300:
                lines.append(f"{ticker} SKIP stale_data age={market_age}s")
                continue

            current_price = float(latest_bar["close"])
            line_parts = [f"{ticker} price={current_price:.2f} qty={position.quantity}"]

            # Idempotency: skip if another process already sold this ticker
            if _recently_exited(ticker):
                lines.append(f"{ticker} SKIP recently-exited-cooldown")
                continue

            # Exit priority 1: EOD exit
            if settings.app.exit_at_eod:
                from trading_bot.runtime.session import should_eod_exit
                if should_eod_exit(now, settings):
                    _close_position(
                        ticker=ticker,
                        position=position,
                        broker=broker,
                        ledger=ledger,
                        current_price=current_price,
                        settings=settings,
                        line_parts=line_parts,
                        exit_events=exit_events,
                        log_path=log_path,
                        reason="eod_exit",
                        state=state,
                    )
                    continue

            # Exit priority 2: Stop loss
            if position.stop_loss is not None and current_price <= position.stop_loss:
                _close_position(
                    ticker=ticker,
                    position=position,
                    broker=broker,
                    ledger=ledger,
                    current_price=current_price,
                    settings=settings,
                    line_parts=line_parts,
                    exit_events=exit_events,
                    log_path=log_path,
                    reason="stop_loss",
                    state=state,
                )
                continue

            # Exit priority 3: Profit target
            if position.profit_target is not None and current_price >= position.profit_target:
                _close_position(
                    ticker=ticker,
                    position=position,
                    broker=broker,
                    ledger=ledger,
                    current_price=current_price,
                    settings=settings,
                    line_parts=line_parts,
                    exit_events=exit_events,
                    log_path=log_path,
                    reason="profit_target",
                    state=state,
                )
                continue

            # Exit priority 4: Counter-thesis exit (V3)
            if getattr(settings, "counter_thesis", None) is not None and settings.counter_thesis.enabled:
                signal, _, details = _build_signal_result(ticker, settings)
                if signal is not None:
                    counter_result = _evaluate_counter_thesis_for_signal(ticker, signal, settings)
                    if counter_result is not None and counter_result.exit_triggered:
                        _close_position(
                            ticker=ticker,
                            position=position,
                            broker=broker,
                            ledger=ledger,
                            current_price=current_price,
                            settings=settings,
                            line_parts=line_parts,
                            exit_events=exit_events,
                            log_path=log_path,
                            reason="counter_thesis",
                            state=state,
                        )
                        continue

            # Exit priority 5: Trailing stop
            atr = _fetch_atr(ticker, settings) if settings.risk.use_atr_sizing else None
            trailing_stop = next_trailing_stop(
                position=position,
                current_price=current_price,
                atr=atr,
                atr_multiplier=settings.risk.atr_trailing_stop_multiplier,
            )
            if trailing_stop is not None and current_price <= trailing_stop:
                _close_position(
                    ticker=ticker,
                    position=position,
                    broker=broker,
                    ledger=ledger,
                    current_price=current_price,
                    settings=settings,
                    line_parts=line_parts,
                    exit_events=exit_events,
                    log_path=log_path,
                    reason="trailing_stop",
                    state=state,
                )
                continue

            # No exit triggered - update highest_high
            if position.highest_high is None or current_price > position.highest_high:
                updated_position = Position(
                    ticker=ticker,
                    quantity=position.quantity,
                    average_cost=position.average_cost,
                    stop_loss=position.stop_loss,
                    profit_target=position.profit_target,
                    highest_high=current_price,
                    initial_risk=position.initial_risk,
                    entry_at=position.entry_at,
                    strategy_tag=position.strategy_tag,
                )
                state.positions[ticker] = updated_position

            line_parts.append(f"highest_high={position.highest_high:.2f if position.highest_high else 'N/A'}")
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
        settings.app.portfolio_path,
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
) -> None:
    """Close a position and record the fill."""
    estimated_fill_price = apply_slippage(current_price, broker.slippage_bps, "SELL")
    fill = submit_signal_as_order(
        signal=None,
        broker=broker,
        account_equity=broker.cash + (current_price * position.quantity),
        open_tickers=set(),
        risk_settings=settings.risk,
        ticker=ticker,
        quantity=position.quantity,
        side="SELL",
        price=estimated_fill_price,
    )

    if fill is None:
        line_parts.append(f"SELL_FAILED reason={reason}")
        return

    ledger.record_fill(fill, side="SELL")
    updated_state = _portfolio_state_from_broker(
        broker,
        None,
        previous_state=state,
        fill_fees=fill.fees,
        filled_at=fill.filled_at,
    )
    # Record exit timestamp for idempotency guard against concurrent sells
    if state is not None:
        updated_state.last_exited_at = dict(state.last_exited_at)
        updated_state.last_exited_at[ticker] = fill.filled_at.isoformat()
    ledger.save_portfolio_state(updated_state)
    ledger.record_equity_snapshot(updated_state, timestamp=fill.filled_at)

    append_decision_event(
        log_path,
        {
            "command": "manage-positions",
            "ticker": ticker,
            "status": "FILLED",
            "reason": reason,
            "quantity": fill.quantity,
            "fill_price": fill.fill_price,
            "fees": fill.fees,
        },
    )

    line_parts.append(f"SELL qty={fill.quantity} price={fill.fill_price:.2f} reason={reason}")
    exit_events.append({
        "ticker": ticker,
        "reason": reason,
        "quantity": fill.quantity,
        "fill_price": fill.fill_price,
    })


def run_continuous_loop(
    settings: Settings,
    interval_seconds: int = 300,
    max_cycles: int | None = None,
    build_universe: bool = True,
    dry_run: bool = False,
    max_failures: int = 10,
    use_event_system: bool = False,
) -> LoopStats:
    """Run the continuous paper-trading loop.

    Each cycle:
    1. SYSTEM_TICK event
    2. Build universe (optional)
    3. Run scan on watchlist
    4. Run paper-trade on approved signals
    5. Run manage-positions for existing positions
    6. Sleep for interval

    Args:
        settings: Application settings
        interval_seconds: Seconds between cycles
        max_cycles: Maximum number of cycles (None = infinite)
        build_universe: Whether to refresh universe each cycle
        dry_run: Preview trades without executing
        max_failures: Consecutive failures before circuit breaker
        use_event_system: Wire events through the event bus

    Returns:
        LoopStats with statistics from the run
    """
    stats = LoopStats()
    stats.start_time = time.monotonic()

    loop: EventLoop | None = None
    bus: MessageBus | None = None
    cache: Cache | None = None

    if use_event_system:
        loop, bus, cache = create_event_orchestrator(settings)

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

        # Emit SYSTEM_TICK if event system is active
        if loop is not None:
            loop.submit(SystemTickEvent(tick=cycle))

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

            if loop is not None and bus is not None:
                for candidate in approved:
                    bus.publish(
                        StrategySignalEvent(
                            ticker=candidate["ticker"],
                            action="BUY",
                            entry_price=candidate.get("entry", 0.0),
                            stop_loss=candidate.get("stop", 0.0),
                            profit_target=candidate.get("target", 0.0),
                            confidence=candidate.get("confidence", 0.0),
                            risk_reward_ratio=candidate.get("rr", 0.0),
                            reasons=candidate.get("reasons", []),
                            timestamp=datetime.now(),
                            timeframe="intraday",
                            strategy_tag="",
                        )
                    )

            stats.log_rejections(
                scan_result.get("summary", {}).get("rejected", 0)
            )

            # Phase 3: Paper trade
            approved_symbols = [c["ticker"] for c in approved]
            if approved_symbols:
                trade_results = run_paper_trade(approved_symbols, settings, dry_run=dry_run)
                trade_count = sum(1 for r in trade_results if "FILLED" in r or "DRY_RUN" in r)
                stats.log_trades(trade_count)
                logger.info(f"paper_trades executed trades={trade_count} results={len(trade_results)}")
            else:
                logger.info("no_approved_signals skipping_paper_trade")

            # Phase 4: Manage positions
            manage_result = _run_manage_positions_once(settings, ledger)
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

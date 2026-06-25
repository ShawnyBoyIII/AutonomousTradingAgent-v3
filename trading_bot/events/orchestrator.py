from __future__ import annotations

from pathlib import Path
from datetime import datetime

from trading_bot.config.settings import Settings
from trading_bot.events.bus import MessageBus
from trading_bot.events.cache import Cache
from trading_bot.events.loop import EventLoop
from trading_bot.events.types import (
    Event,
    MarketBarEvent,
    OrderFillEvent,
    OrderRejectEvent,
    OrderRequestEvent,
    PortfolioStateEvent,
    PortfolioPnLEvent,
    RiskDecisionEvent,
    StrategySignalEvent,
    SystemTickEvent,
)
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.models.portfolio import PortfolioState


class EventHandler:
    """Base class for event-driven handlers in the trading system."""

    def __init__(self, bus: MessageBus, cache: Cache) -> None:
        self.bus = bus
        self.cache = cache

    def handle(self, event: Event) -> None:
        raise NotImplementedError


class SignalHandler(EventHandler):
    """Processes strategy signals and routes them to risk management."""

    def __init__(self, bus: MessageBus, cache: Cache, settings: Settings) -> None:
        super().__init__(bus, cache)
        self.settings = settings

    def register(self) -> None:
        self.bus.subscribe("STRATEGY_SIGNAL", self.on_signal)

    def on_signal(self, event: StrategySignalEvent) -> None:
        if event.action == "HOLD":
            return

        decision = self._evaluate_risk(event)
        if decision.approved:
            self.bus.publish_to(
                "ORDER_REQUEST",
                OrderRequestEvent(
                    order_id=f"evt_{event.ticker}_{int(event.timestamp.timestamp())}",
                    ticker=event.ticker,
                    side="BUY",
                    order_type="market",
                    quantity=decision.position_size,
                    strategy_tag=event.strategy_tag,
                    timestamp=event.timestamp,
                ),
            )
        else:
            self.bus.publish_to(
                "ORDER_REJECT",
                OrderRejectEvent(
                    order_id=f"evt_{event.ticker}_{int(event.timestamp.timestamp())}",
                    ticker=event.ticker,
                    reason=decision.reason,
                    timestamp=event.timestamp,
                ),
            )

    def _evaluate_risk(self, signal: StrategySignalEvent) -> RiskDecisionEvent:
        from trading_bot.risk.risk_manager import evaluate_signal
        from trading_bot.models.signal import TradeSignal

        trade_signal = TradeSignal(
            ticker=signal.ticker,
            timeframe=signal.timeframe,
            action=signal.action,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            profit_target=signal.profit_target,
            risk_reward_ratio=signal.risk_reward_ratio,
            confidence=signal.confidence,
            reasons=signal.reasons,
            strategy_tag=signal.strategy_tag,
            timestamp=signal.timestamp,
        )

        state = self.cache.get_position(signal.ticker)
        open_tickers = self.cache.get_open_positions()

        decision = evaluate_signal(
            signal=trade_signal,
            account_equity=self.cache.get_equity(),
            open_tickers=open_tickers,
            portfolio_heat_pct=0.0,
            atr=None,
            risk_settings=self.settings.risk,
            counter_thesis=None,
        )

        return RiskDecisionEvent(
            ticker=signal.ticker,
            approved=decision.approved,
            position_size=decision.position_size,
            dollar_risk=decision.dollar_risk,
            reason=decision.reason,
            timestamp=signal.timestamp,
        )


class OrderHandler(EventHandler):
    """Processes order requests and emits fills or rejections."""

    def __init__(self, bus: MessageBus, cache: Cache, settings: Settings) -> None:
        super().__init__(bus, cache)
        self.settings = settings

    def register(self) -> None:
        self.bus.subscribe("ORDER_REQUEST", self.on_order_request)

    def on_order_request(self, event: OrderRequestEvent) -> None:
        from trading_bot.execution.paper_broker import PaperBroker

        state = self.cache.get_position(event.ticker)
        if state and state.get("quantity", 0) > 0:
            self.bus.publish_to(
                "ORDER_REJECT",
                OrderRejectEvent(
                    order_id=event.order_id,
                    ticker=event.ticker,
                    reason="duplicate open ticker",
                    timestamp=event.timestamp,
                ),
            )
            return

        position_size = event.quantity
        entry_price = 150.0
        total_cost = (entry_price * position_size) + 1.0
        if self.cache.get_cash() < total_cost:
            self.bus.publish_to(
                "ORDER_REJECT",
                OrderRejectEvent(
                    order_id=event.order_id,
                    ticker=event.ticker,
                    reason="insufficient cash",
                    timestamp=event.timestamp,
                ),
            )
            return

        fill = OrderFillEvent(
            order_id=event.order_id,
            ticker=event.ticker,
            quantity=position_size,
            fill_price=entry_price,
            fees=1.0,
            side="BUY",
            timestamp=event.timestamp,
        )
        self.bus.publish_to("ORDER_FILL", fill)


class PortfolioHandler(EventHandler):
    """Updates portfolio state from fills and other events."""

    def __init__(self, bus: MessageBus, cache: Cache, ledger: PortfolioLedger) -> None:
        super().__init__(bus, cache)
        self.ledger = ledger

    def register(self) -> None:
        self.bus.subscribe("ORDER_FILL", self.on_fill)
        self.bus.subscribe("ORDER_REJECT", self.on_reject)

    def on_fill(self, event: OrderFillEvent) -> None:
        self.cache.update_from_fill(event)
        new_cash = self.cache.get_cash() - (event.fill_price * event.quantity) - event.fees
        state = PortfolioState(
            cash=round(new_cash, 2),
            equity=round(new_cash + self.cache.get_exposure(), 2),
            positions=self.cache.positions,
            realized_pnl=self.cache.realized_pnl,
        )
        self.ledger.save_portfolio_state(state)
        self.bus.publish_to(
            "PORTFOLIO_STATE",
            PortfolioStateEvent(
                cash=state.cash,
                equity=state.equity,
                positions={k: v for k, v in state.positions.items()},
                realized_pnl=state.realized_pnl,
                timestamp=event.timestamp,
            ),
        )

    def on_reject(self, event: OrderRejectEvent) -> None:
        self.cache.update_from_reject(event)


class MarketDataHandler(EventHandler):
    """Processes market data bars and updates cache."""

    def __init__(self, bus: MessageBus, cache: Cache) -> None:
        super().__init__(bus, cache)

    def register(self) -> None:
        self.bus.subscribe("MARKET_BAR", self.on_bar)

    def on_bar(self, event: MarketBarEvent) -> None:
        self.cache.update_from_bar(event)


def create_event_orchestrator(
    settings: Settings,
    loop: EventLoop | None = None,
) -> tuple[EventLoop, MessageBus, Cache]:
    """Create a fully wired event-driven orchestrator.

    Returns (event_loop, bus, cache) ready to process events.
    """
    bus = MessageBus()
    cache = Cache()
    loop = loop or EventLoop(bus)

    ledger = PortfolioLedger(Path(settings.app.state_db_path))
    initial_state = ledger.ensure_portfolio_state()
    cache.cash = initial_state.cash
    cache.equity = initial_state.equity

    SignalHandler(bus, cache, settings).register()
    OrderHandler(bus, cache, settings).register()
    PortfolioHandler(bus, cache, ledger).register()
    MarketDataHandler(bus, cache).register()

    return loop, bus, cache

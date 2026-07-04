from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import gymnasium as gym
import numpy as np

from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.order import OrderRequest

logger = logging.getLogger(__name__)


class ActionScheme(ABC):
    def __init__(self) -> None:
        self._action_space: gym.spaces.Space | None = None

    @property
    def action_space(self) -> gym.spaces.Space:
        if self._action_space is None:
            raise NotImplementedError("action_space not initialized")
        return self._action_space

    @abstractmethod
    def perform(self, action: Any, prices: dict[str, float]) -> None: ...

    def reset(self) -> None:
        pass

    def reset_portfolio(self, broker: PaperBroker | None) -> None:
        pass


class BSHActionScheme(ActionScheme):
    """Buy/Sell/Hold action scheme.

    Each action maps to (symbol, direction):
    - direction 0 = HOLD (no-op)
    - direction 1 = BUY (max affordable shares)
    - direction 2 = SELL (all shares)

    Action space: Discrete(n_symbols * 3 + 1) where +1 is global no-op.
    """

    def __init__(self, symbols: list[str], max_shares: int = 100) -> None:
        super().__init__()
        self.symbols = [s.upper().strip() for s in symbols]
        self.max_shares = max_shares
        n_actions = len(symbols) * 3 + 1
        self._action_space = gym.spaces.Discrete(n_actions)
        self._broker: PaperBroker | None = None

    def reset_portfolio(self, broker: PaperBroker | None) -> None:
        self._broker = broker

    def perform(self, action: int, prices: dict[str, float]) -> None:
        if self._broker is None or action == 0:
            return

        action -= 1
        symbol_idx = action // 3
        direction = action % 3
        symbol = self.symbols[symbol_idx]

        current_pos = self._broker.positions.get(symbol, 0)
        price = prices.get(symbol)
        if price is None or price <= 0:
            return

        if direction == 1:
            self._buy(symbol, price)
        elif direction == 2 and current_pos > 0:
            self._sell(symbol, price, current_pos)

    def _buy(self, symbol: str, price: float) -> None:
        if self._broker is None:
            return

        affordable = int(self._broker.cash * 0.95 / price)
        shares = min(affordable, self.max_shares)
        if shares < 1:
            return

        order = OrderRequest(
            ticker=symbol,
            side="BUY",
            order_type="market",
            quantity=shares,
            submitted_at=datetime.now(),
        )
        try:
            self._broker.submit_order(order, price)
        except ValueError as e:
            logger.debug("Order skipped: %s", e)

    def _sell(self, symbol: str, price: float, quantity: int) -> None:
        if self._broker is None:
            return

        order = OrderRequest(
            ticker=symbol,
            side="SELL",
            order_type="market",
            quantity=quantity,
            submitted_at=datetime.now(),
        )
        try:
            self._broker.submit_order(order, price)
        except ValueError as e:
            logger.debug("Order skipped: %s", e)


class ProportionActionScheme(ActionScheme):
    """Proportion-based action scheme.

    Action encoding: (symbol_idx, direction, proportion_bucket)
    - symbol_idx: which symbol to trade
    - direction: 0=BUY, 1=SELL
    - proportion_bucket: 0-9 representing 10%, 20%, ..., 100% of available

    Action space: Discrete(n_symbols * 2 * 10 + 1)
    """

    PROPORTIONS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]

    def __init__(self, symbols: list[str], max_shares: int = 500) -> None:
        super().__init__()
        self.symbols = [s.upper().strip() for s in symbols]
        self.max_shares = max_shares
        n_actions = len(symbols) * 2 * len(self.PROPORTIONS) + 1
        self._action_space = gym.spaces.Discrete(n_actions)
        self._broker: PaperBroker | None = None

    def reset_portfolio(self, broker: PaperBroker | None) -> None:
        self._broker = broker

    def perform(self, action: int, prices: dict[str, float]) -> None:
        if self._broker is None or action == 0:
            return

        action -= 1
        symbol_idx = action // 20
        direction = (action // 10) % 2
        prop_idx = action % 10
        symbol = self.symbols[symbol_idx]
        proportion = self.PROPORTIONS[prop_idx]

        current_pos = self._broker.positions.get(symbol, 0)
        price = prices.get(symbol)
        if price is None or price <= 0:
            return

        if direction == 0:
            self._buy_proportion(symbol, price, proportion)
        elif direction == 1 and current_pos > 0:
            sell_qty = int(current_pos * proportion)
            if sell_qty > 0:
                self._sell(symbol, price, sell_qty)

    def _buy_proportion(self, symbol: str, price: float, proportion: float) -> None:
        if self._broker is None:
            return

        alloc = self._broker.cash * proportion
        shares = int(alloc / price)
        shares = min(shares, self.max_shares)
        if shares < 1:
            return

        order = OrderRequest(
            ticker=symbol,
            side="BUY",
            order_type="market",
            quantity=shares,
            submitted_at=datetime.now(),
        )
        try:
            self._broker.submit_order(order, price)
        except ValueError as e:
            logger.debug("Order skipped: %s", e)

    def _sell(self, symbol: str, price: float, quantity: int) -> None:
        if self._broker is None:
            return

        order = OrderRequest(
            ticker=symbol,
            side="SELL",
            order_type="market",
            quantity=quantity,
            submitted_at=datetime.now(),
        )
        try:
            self._broker.submit_order(order, price)
        except ValueError as e:
            logger.debug("Order skipped: %s", e)

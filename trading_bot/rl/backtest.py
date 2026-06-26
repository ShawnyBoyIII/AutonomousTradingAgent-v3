from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from trading_bot.backtest.diagnostics import diagnostics
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.rl.features import (
    FEATURE_COLS,
    build_market_feature_row,
    build_observation,
    build_portfolio_feature_row,
)

logger = logging.getLogger(__name__)


@dataclass
class RLBacktestConfig:
    model_path: str | None = None
    symbols: list[str] = field(default_factory=lambda: ["AAPL"])
    starting_cash: float = 10_000.0
    fee_per_order: float = 1.0
    slippage_bps: int = 0
    max_positions: int = 10
    max_shares: int = 100
    observer_window: int = 10
    prediction_mode: str = "deterministic"
    bar_period: str = "1y"
    bar_interval: str = "1d"
    max_symbols: int | None = None
    use_intraday_exit: bool = False
    stop_loss_pct: float = 0.03
    profit_target_pct: float = 0.03


class RLBacktestRunner:
    """Runs RL agent inference on pre-loaded market data for backtesting.

    Unlike the training environment, this runner:
    - Takes pre-loaded data frames (no network calls)
    - Builds observations from in-memory data
    - Maps RL actions to trading decisions
    - Resolves exits using the same logic as the rule-based backtest
    """

    FEATURE_COLS = FEATURE_COLS

    def __init__(self, config: RLBacktestConfig | None = None) -> None:
        self.config = config or RLBacktestConfig()
        self._model = None
        self._data_cache: dict[str, pd.DataFrame] = {}
        self._data_indices: dict[str, int] = {}

    def load_model(self, model_path: str | None = None) -> None:
        path = model_path or self.config.model_path
        if path is None:
            raise ValueError("No model path specified. Provide model_path or set config.model_path")

        try:
            from stable_baselines3 import PPO
        except ImportError:
            raise ImportError("stable-baselines3 required for RL backtest")

        self._model = PPO.load(path)
        logger.info(f"RL model loaded from {path}")

    def set_model(self, model: Any) -> None:
        self._model = model

    def _load_symbols(self, symbols: list[str], daily_frame: pd.DataFrame,
                      intraday_frame: pd.DataFrame | None = None) -> None:
        for symbol in symbols:
            self._data_cache[symbol] = daily_frame.copy()
            self._data_indices[symbol] = 0

    # ------------------------------------------------------------------ #
    #  Frame preparation                                                  #
    # ------------------------------------------------------------------ #

    def _prepare_frames(
        self,
        symbol: str | None,
        daily_frame: pd.DataFrame | None,
        daily_frames: dict[str, pd.DataFrame] | None,
        intraday_frame: pd.DataFrame | None,
        intraday_frames: dict[str, pd.DataFrame] | None,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], list[str], str, pd.DataFrame] | None:
        """Build per-symbol frame dicts, resolve the master timeline, and validate.

        Returns (frames, intra_frames, all_symbols, primary, master_frame) or
        None if there isn't enough data to backtest.
        """
        frames: dict[str, pd.DataFrame] = {}
        if daily_frames is not None:
            frames.update({k: v for k, v in daily_frames.items() if v is not None and not v.empty})
        if daily_frame is not None and not daily_frame.empty and symbol:
            frames[symbol] = daily_frame

        if not frames:
            return None

        intra_frames: dict[str, pd.DataFrame] = {}
        if intraday_frames is not None:
            intra_frames.update({k: v for k, v in intraday_frames.items() if v is not None and not v.empty})
        if intraday_frame is not None and not intraday_frame.empty and symbol:
            intra_frames[symbol] = intraday_frame

        all_symbols = self.config.symbols
        if not all_symbols:
            all_symbols = list(frames.keys())

        primary = symbol if symbol and symbol in frames else all_symbols[0]
        master_frame = intra_frames.get(primary)
        if master_frame is None:
            master_frame = frames.get(primary)
        if master_frame is None or len(master_frame) < self.config.observer_window + 5:
            return None

        return frames, intra_frames, all_symbols, primary, master_frame

    # ------------------------------------------------------------------ #
    #  Feature computation & observation                                  #
    # ------------------------------------------------------------------ #

    def _compute_features_for_symbol(self, symbol: str) -> list[float]:
        df = self._data_cache.get(symbol)
        if df is None or df.empty:
            return [0.0] * len(self.FEATURE_COLS)

        idx = self._data_indices.get(symbol, 0)
        if idx >= len(df):
            return [0.0] * len(self.FEATURE_COLS)

        return build_market_feature_row(df.iloc[: idx + 1].copy())

    def _build_observation(self, symbol: str, window: pd.DataFrame,
                           portfolio_state: PortfolioState) -> np.ndarray:
        return build_observation(
            [build_market_feature_row(window.copy())],
            build_portfolio_feature_row(portfolio_state),
            observer_window=self.config.observer_window,
        )

    def _build_observation_batch(
        self,
        all_symbols: list[str],
        portfolio_state: PortfolioState,
    ) -> np.ndarray:
        """Build an observation from all symbol features + portfolio state."""
        market_rows = [self._compute_features_for_symbol(sym) for sym in all_symbols]
        max_sym = self.config.max_symbols or len(all_symbols)
        while len(market_rows) < max_sym:
            market_rows.append([0.0] * len(self.FEATURE_COLS))
        return build_observation(
            market_rows[:max_sym],
            build_portfolio_feature_row(portfolio_state),
            observer_window=self.config.observer_window,
        )

    # ------------------------------------------------------------------ #
    #  Model inference                                                    #
    # ------------------------------------------------------------------ #

    def _predict_action(self, observation: np.ndarray) -> int:
        if self._model is None:
            raise RuntimeError("No model loaded. Call load_model() or set_model() first.")

        deterministic = self.config.prediction_mode == "deterministic"
        action, _ = self._model.predict(observation, deterministic=deterministic)
        return int(action)

    # ------------------------------------------------------------------ #
    #  Action decoding (BSH: 1 + 3 × N symbols)                           #
    #  0 = global HOLD                                                    #
    #  1 + sym_idx*3 + 0 = HOLD,  1 = BUY,  2 = SELL                     #
    # ------------------------------------------------------------------ #

    def _decode_action(self, action: int) -> tuple[str | None, int]:
        """Return (target_symbol, direction) where direction is 0=HOLD, 1=BUY, 2=SELL."""
        if action == 0:
            return None, 0
        symbols = self.config.symbols
        if not symbols:
            return None, 0
        action_idx = action - 1
        symbol_idx = action_idx // 3
        direction = action_idx % 3
        if symbol_idx >= len(symbols):
            return None, 0
        return symbols[symbol_idx], direction

    def _action_to_target_symbol(self, action: int, symbols: list[str]) -> str | None:
        target, direction = self._decode_action(action)
        return target if direction != 0 else None

    def _action_to_trade(self, action: int, symbol: str, prices: dict[str, float],
                          broker: PaperBroker,
                          trade_symbols: set[str] | None = None) -> tuple[str | None, float | None]:
        target_symbol, direction = self._decode_action(action)
        if target_symbol is None or direction == 0:
            return None, None
        if trade_symbols is not None and target_symbol not in trade_symbols:
            return None, None

        price = prices.get(target_symbol)
        if price is None or price <= 0:
            return None, None

        if direction == 1:
            return "BUY", price
        if direction == 2:
            return ("SELL", price) if broker.positions.get(target_symbol, 0) > 0 else (None, None)
        return None, None

    # ------------------------------------------------------------------ #
    #  Exit resolution                                                    #
    # ------------------------------------------------------------------ #

    def _resolve_exit(self, intraday_frame: pd.DataFrame, entry_index: int,
                      stop_loss: float, profit_target: float) -> tuple[float, int]:
        after = intraday_frame.iloc[entry_index + 1:]
        if after.empty:
            return float(intraday_frame.iloc[entry_index]["close"]), entry_index

        for row_index in range(entry_index + 1, len(intraday_frame)):
            row = intraday_frame.iloc[row_index]
            if float(row["low"]) <= stop_loss:
                return stop_loss, row_index
            if float(row["high"]) >= profit_target:
                return profit_target, row_index

        last_idx = min(entry_index + len(after) - 1, len(intraday_frame) - 1)
        return float(intraday_frame.iloc[last_idx]["close"]), last_idx

    # ------------------------------------------------------------------ #
    #  Trade execution                                                    #
    # ------------------------------------------------------------------ #

    def _execute_buy(
        self,
        target_symbol: str,
        entry_price: float,
        broker: PaperBroker,
        entry_cost_basis: dict[str, float],
        intra_frames: dict[str, pd.DataFrame],
        frames: dict[str, pd.DataFrame],
        master_frame: pd.DataFrame,
        entry_index: int,
    ) -> tuple[float, int, int] | None:
        """Execute a BUY and resolve exit. Returns (pnl, exit_index, shares) or None."""
        affordable = int(broker.cash * 0.95 / entry_price)
        shares = min(affordable, self.config.max_shares)
        if shares < 1:
            return None

        entry_value = entry_price * shares
        broker.cash -= entry_value + self.config.fee_per_order
        broker.positions[target_symbol] = shares
        entry_cost_basis[target_symbol] = entry_price

        stop_loss = entry_price * (1.0 - self.config.stop_loss_pct)
        profit_target = entry_price * (1.0 + self.config.profit_target_pct)

        target_intraday = intra_frames.get(target_symbol)
        if target_intraday is None:
            target_intraday = frames.get(target_symbol)
        if target_intraday is None:
            target_intraday = master_frame
        exit_price, exit_index = self._resolve_exit(
            target_intraday, entry_index, stop_loss, profit_target
        )

        exit_value = exit_price * shares
        trade_pnl = exit_value - entry_value - 2 * self.config.fee_per_order

        broker.cash += exit_value - self.config.fee_per_order
        broker.positions.pop(target_symbol, None)
        entry_cost_basis.pop(target_symbol, None)
        return trade_pnl, exit_index, shares

    def _execute_sell(
        self,
        target_symbol: str,
        sell_price: float,
        broker: PaperBroker,
        entry_cost_basis: dict[str, float],
        prices: dict[str, float],
    ) -> float | None:
        """Execute a SELL and return the PnL, or None if no position."""
        current_pos = broker.positions.get(target_symbol, 0)
        if current_pos <= 0:
            return None

        price = prices.get(target_symbol, sell_price)
        cost_basis = entry_cost_basis.get(target_symbol, price)
        exit_value = price * current_pos
        trade_pnl = exit_value - (current_pos * cost_basis) - self.config.fee_per_order

        broker.cash += exit_value - self.config.fee_per_order
        broker.positions.pop(target_symbol, None)
        entry_cost_basis.pop(target_symbol, None)
        return trade_pnl

    def _build_portfolio_state(
        self,
        broker: PaperBroker,
        prices: dict[str, float],
        entry_cost_basis: dict[str, float],
    ) -> PortfolioState:
        return PortfolioState(
            cash=broker.cash,
            equity=broker.cash + sum(
                qty * prices.get(tkr, 0) for tkr, qty in broker.positions.items()
            ),
            positions={
                tkr: Position(ticker=tkr, quantity=qty, average_cost=entry_cost_basis.get(tkr, 0))
                for tkr, qty in broker.positions.items()
            },
        )

    # ------------------------------------------------------------------ #
    #  Main entry point                                                   #
    # ------------------------------------------------------------------ #

    def run_backtest(
        self,
        symbol: str | None = None,
        daily_frame: pd.DataFrame | None = None,
        daily_frames: dict[str, pd.DataFrame] | None = None,
        intraday_frame: pd.DataFrame | None = None,
        intraday_frames: dict[str, pd.DataFrame] | None = None,
        starting_cash: float | None = None,
        trade_symbols: list[str] | None = None,
    ) -> dict[str, Any]:
        prepared = self._prepare_frames(
            symbol, daily_frame, daily_frames, intraday_frame, intraday_frames,
        )
        if prepared is None:
            return {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "rl_actions": []}

        frames, intra_frames, all_symbols, primary, master_frame = prepared

        if not self.config.use_intraday_exit:
            intra_frames = {}
            master_frame = frames.get(primary, master_frame)

        cash = starting_cash or self.config.starting_cash
        broker = PaperBroker(
            starting_cash=cash,
            fee_per_order=self.config.fee_per_order,
            slippage_bps=self.config.slippage_bps,
        )
        trades = 0
        wins = 0
        losses = 0
        net_pnl = 0.0
        gross_profit = 0.0
        gross_loss = 0.0
        entry_blocked_until = -1
        rl_actions: list[dict[str, int]] = []
        entry_cost_basis: dict[str, float] = {}
        allowed_symbols = set(trade_symbols) if trade_symbols else None

        for sym in all_symbols:
            self._data_cache[sym] = frames.get(sym, frames[primary]).copy()
            self._data_indices[sym] = 0

        for end_index in range(self.config.observer_window, len(master_frame)):
            if end_index <= entry_blocked_until:
                continue

            prices: dict[str, float] = {}
            for sym in all_symbols:
                df = self._data_cache.get(sym)
                if df is not None and end_index < len(df):
                    prices[sym] = float(df.iloc[end_index]["close"])

            portfolio_state = self._build_portfolio_state(broker, prices, entry_cost_basis)
            observation = self._build_observation_batch(all_symbols, portfolio_state)

            action = self._predict_action(observation)
            rl_actions.append({"step": end_index, "action": action})

            trade_type, trade_price = self._action_to_trade(
                action, primary, prices, broker, trade_symbols=allowed_symbols,
            )

            if trade_type == "BUY" and trade_price is not None:
                target_symbol = self._action_to_target_symbol(action, all_symbols)
                if target_symbol is None:
                    continue
                price = prices.get(target_symbol, trade_price)
                if price <= 0:
                    continue
                result = self._execute_buy(
                    target_symbol, price, broker, entry_cost_basis,
                    intra_frames, frames, master_frame, end_index,
                )
                if result is not None:
                    trade_pnl, exit_index, _shares = result
                    net_pnl += trade_pnl
                    trades += 1
                    if trade_pnl > 0:
                        gross_profit += trade_pnl
                        wins += 1
                    else:
                        gross_loss += trade_pnl
                        losses += 1
                    entry_blocked_until = exit_index

            elif trade_type == "SELL" and trade_price is not None:
                target_symbol = self._action_to_target_symbol(action, all_symbols)
                if target_symbol is None:
                    continue
                trade_pnl = self._execute_sell(
                    target_symbol, trade_price, broker, entry_cost_basis, prices,
                )
                if trade_pnl is not None:
                    net_pnl += trade_pnl
                    trades += 1
                    if trade_pnl > 0:
                        gross_profit += trade_pnl
                        wins += 1
                    else:
                        gross_loss += trade_pnl
                        losses += 1
                    entry_blocked_until = end_index

            for sym in all_symbols:
                if sym in self._data_indices:
                    self._data_indices[sym] = end_index + 1

        return {
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "net_pnl": round(net_pnl, 2),
            "win_rate": 0.0 if trades == 0 else wins / trades,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "rl_actions": rl_actions,
            **diagnostics(
                trades=trades,
                wins=wins,
                losses=losses,
                net_pnl=net_pnl,
                gross_profit=gross_profit,
                gross_loss=gross_loss,
            ),
        }

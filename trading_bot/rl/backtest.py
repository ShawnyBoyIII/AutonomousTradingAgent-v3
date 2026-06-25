from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from trading_bot.data.indicators import (
    add_ema, add_rsi, add_sma, add_atr, add_macd,
    add_bollinger_bands, add_atr_percent,
)
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.portfolio import PortfolioState, Position

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


class RLBacktestRunner:
    """Runs RL agent inference on pre-loaded market data for backtesting.

    Unlike the training environment, this runner:
    - Takes pre-loaded data frames (no network calls)
    - Builds observations from in-memory data
    - Maps RL actions to trading decisions
    - Resolves exits using the same logic as the rule-based backtest
    """

    FEATURE_COLS = [
        "close", "return_1d", "rsi_14", "ema_12", "ema_26",
        "sma_20", "macd_line", "macd_signal", "macd_histogram",
        "bb_percent_b", "bb_width", "atr_pct", "volume_ratio",
    ]

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

    def _build_observation(self, symbol: str, window: pd.DataFrame,
                           portfolio_state: PortfolioState) -> np.ndarray:
        df = window.copy()
        n_features = len(self.FEATURE_COLS)

        df = add_ema(df, 12, "ema_12")
        df = add_ema(df, 26, "ema_26")
        df = add_rsi(df, 14)
        df = add_sma(df, 20, "sma_20")
        df = add_macd(df, 12, 26, 9)
        df = add_bollinger_bands(df, 20, 2.0)
        df = add_atr_percent(df, 14)

        close_col = float(df["close"].iloc[-1]) if "close" in df.columns else 0.0
        returns = 0.0
        if len(df) >= 2:
            prev_close = float(df["close"].iloc[-2])
            if prev_close > 0:
                returns = (close_col - prev_close) / prev_close

        rsi = float(df["rsi_14"].iloc[-1]) if "rsi_14" in df.columns and pd.notna(df["rsi_14"].iloc[-1]) else 50.0
        ema_12 = float(df["ema_12"].iloc[-1]) if "ema_12" in df.columns and pd.notna(df["ema_12"].iloc[-1]) else close_col
        ema_26 = float(df["ema_26"].iloc[-1]) if "ema_26" in df.columns and pd.notna(df["ema_26"].iloc[-1]) else close_col
        sma_20 = float(df["sma_20"].iloc[-1]) if "sma_20" in df.columns and pd.notna(df["sma_20"].iloc[-1]) else close_col

        macd_line = float(df["macd_line"].iloc[-1]) if "macd_line" in df.columns and pd.notna(df["macd_line"].iloc[-1]) else 0.0
        macd_signal = float(df["macd_signal"].iloc[-1]) if "macd_signal" in df.columns and pd.notna(df["macd_signal"].iloc[-1]) else 0.0
        macd_hist = float(df["macd_histogram"].iloc[-1]) if "macd_histogram" in df.columns and pd.notna(df["macd_histogram"].iloc[-1]) else 0.0

        bb_pct = float(df["bb_percent_b"].iloc[-1]) if "bb_percent_b" in df.columns and pd.notna(df["bb_percent_b"].iloc[-1]) else 50.0
        bb_w = float(df["bb_width"].iloc[-1]) if "bb_width" in df.columns and pd.notna(df["bb_width"].iloc[-1]) else 0.0
        atr_pct = float(df["atr_pct"].iloc[-1]) if "atr_pct" in df.columns and pd.notna(df["atr_pct"].iloc[-1]) else 0.0

        volume = float(df["volume"].iloc[-1]) if "volume" in df.columns else 0.0
        volume_ratio = 1.0
        if len(df) >= 2 and "volume" in df.columns:
            prev_vol = float(df["volume"].iloc[-2])
            if prev_vol > 0:
                volume_ratio = volume / prev_vol

        row = [
            close_col, returns, rsi, ema_12, ema_26,
            sma_20, macd_line, macd_signal, macd_hist,
            bb_pct, bb_w, atr_pct, volume_ratio,
        ]

        equity = max(portfolio_state.equity, 1e-8)
        cash_ratio = portfolio_state.cash / equity
        num_positions = len(portfolio_state.positions)
        position_weight_sum = sum(
            p.quantity * p.average_cost / equity
            for p in portfolio_state.positions.values()
        )
        unrealized_pnl_pct = portfolio_state.unrealized_pnl / equity
        realized_pnl_pct = portfolio_state.realized_pnl / equity

        portfolio_features = [
            cash_ratio, num_positions, position_weight_sum,
            unrealized_pnl_pct, realized_pnl_pct,
        ]

        all_features = row + portfolio_features
        row_padded = [float(v) for v in all_features]

        history_list = [row_padded]
        padding_rows = self.config.observer_window - len(history_list)
        zero_row = [0.0] * len(all_features)
        for _ in range(padding_rows):
            history_list.insert(0, zero_row)

        return np.array(history_list[:self.config.observer_window], dtype=np.float32)

    def _predict_action(self, observation: np.ndarray) -> int:
        if self._model is None:
            raise RuntimeError("No model loaded. Call load_model() or set_model() first.")

        deterministic = self.config.prediction_mode == "deterministic"
        action, _ = self._model.predict(observation, deterministic=deterministic)
        return int(action)

    def _action_to_trade(self, action: int, symbol: str, prices: dict[str, float],
                         broker: PaperBroker) -> tuple[str | None, float | None]:
        if action == 0:
            return None, None

        symbols = self.config.symbols
        if symbol not in symbols:
            return None, None

        action_idx = action - 1
        symbol_idx = action_idx // 3
        direction = action_idx % 3

        if symbol_idx >= len(symbols):
            return None, None

        target_symbol = symbols[symbol_idx]
        price = prices.get(target_symbol)
        if price is None or price <= 0:
            return None, None

        if direction == 1:
            return "BUY", price
        elif direction == 2:
            current_pos = broker.positions.get(target_symbol, 0)
            if current_pos > 0:
                return "SELL", price
            return None, None

        return None, None

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

    def run_backtest(self, symbol: str, daily_frame: pd.DataFrame,
                     intraday_frame: pd.DataFrame | None = None,
                     starting_cash: float | None = None) -> dict[str, Any]:
        if intraday_frame is None:
            intraday_frame = daily_frame

        if len(intraday_frame) < self.config.observer_window + 5:
            return {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "rl_actions": []}

        cash = starting_cash or self.config.starting_cash
        broker = PaperBroker(starting_cash=cash, fee_per_order=self.config.fee_per_order,
                             slippage_bps=self.config.slippage_bps)
        trades = 0
        wins = 0
        losses = 0
        net_pnl = 0.0
        entry_blocked_until = -1
        rl_actions = []

        self._load_symbols(self.config.symbols, daily_frame, intraday_frame)

        for end_index in range(self.config.observer_window, len(intraday_frame)):
            if end_index <= entry_blocked_until:
                continue

            window = intraday_frame.iloc[:end_index + 1]
            prices = {symbol: float(intraday_frame.iloc[end_index]["close"])}

            portfolio_state = PortfolioState(
                cash=broker.cash,
                equity=broker.cash + sum(
                    qty * prices.get(tkr, 0)
                    for tkr, qty in broker.positions.items()
                ),
                positions={
                    tkr: Position(ticker=tkr, quantity=qty, average_cost=0)
                    for tkr, qty in broker.positions.items()
                },
            )

            observation = self._build_observation(symbol, window, portfolio_state)
            action = self._predict_action(observation)
            rl_actions.append({"step": end_index, "action": action})

            trade_type, trade_price = self._action_to_trade(
                action, symbol, prices, broker
            )

            if trade_type == "BUY" and trade_price is not None:
                affordable = int(broker.cash * 0.95 / trade_price)
                shares = min(affordable, self.config.max_shares)
                if shares >= 1:
                    entry_price = trade_price
                    entry_value = entry_price * shares
                    broker.cash -= entry_value + self.config.fee_per_order
                    broker.positions[symbol] = shares

                    stop_loss = entry_price * 0.97
                    profit_target = entry_price * 1.03

                    exit_price, exit_index = self._resolve_exit(
                        intraday_frame, end_index, stop_loss, profit_target
                    )

                    exit_value = exit_price * shares
                    trade_pnl = exit_value - entry_value - 2 * self.config.fee_per_order
                    net_pnl += trade_pnl
                    trades += 1
                    if trade_pnl > 0:
                        wins += 1
                    else:
                        losses += 1

                    broker.cash += exit_value - self.config.fee_per_order
                    broker.positions.pop(symbol, None)
                    entry_blocked_until = exit_index

            elif trade_type == "SELL" and trade_price is not None:
                current_pos = broker.positions.get(symbol, 0)
                if current_pos > 0:
                    exit_value = trade_price * current_pos
                    trade_pnl = exit_value - (current_pos * 0) - self.config.fee_per_order
                    net_pnl += trade_pnl
                    trades += 1
                    if trade_pnl > 0:
                        wins += 1
                    else:
                        losses += 1

                    broker.cash += exit_value - self.config.fee_per_order
                    broker.positions.pop(symbol, None)
                    entry_blocked_until = end_index

        return {
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "net_pnl": round(net_pnl, 2),
            "win_rate": 0.0 if trades == 0 else wins / trades,
            "rl_actions": rl_actions,
        }

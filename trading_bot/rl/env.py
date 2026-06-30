from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

import gymnasium as gym
import numpy as np
import pandas as pd

from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.order import OrderRequest
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.rl.actions import ActionScheme
from trading_bot.rl.observer import Observer
from trading_bot.rl.rewards import (
    CompoundDailyReward,
    DrawdownPenaltyReward,
    RewardScheme,
    RiskAdjustedReward,
    ShannonEntropyReward,
    SharpeReward,
    SimpleProfitReward,
)

logger = logging.getLogger(__name__)


@dataclass
class TradingConfig:
    starting_cash: float = 100_000.0
    fee_per_order: float = 1.0
    slippage_bps: int = 5
    max_positions: int = 10
    min_order_size: int = 1
    max_order_size: int = 1000
    action_scheme: str = "bsh"
    reward_scheme: str = "risk_adjusted"
    reward_scale: float = 100.0
    observer_window: int = 10
    max_symbols: int | None = None
    symbols: list[str] = field(default_factory=lambda: ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"])
    bar_period: str = "1y"
    bar_interval: str = "1d"
    data_end_date: str | None = None
    random_start_pct: float = 0.0
    max_episode_steps: int = 500

    def __post_init__(self) -> None:
        if isinstance(self.action_scheme, str):
            self.action_scheme = self.action_scheme.lower()
        if isinstance(self.reward_scheme, str):
            self.reward_scheme = self.reward_scheme.lower()


@dataclass(frozen=True)
class EpisodeSummary:
    steps: int
    starting_equity: float
    ending_equity: float
    total_reward: float
    total_return_pct: float
    trade_count: int
    buy_count: int
    sell_count: int
    open_positions: int


class TradingEnv(gym.Env):
    """Gymnasium-compatible trading environment.

    Inspired by TensorTrade architecture:
    - ActionScheme converts agent output to orders
    - Observer generates observations from market data + portfolio state
    - RewardScheme computes learning signal from portfolio performance

    Safety constraints enforced:
    - Position sizing capped by available cash
    - Max positions limit
    - No trading on invalid data
    """

    metadata = {"render_modes": [None], "render_fps": 1}

    def __init__(self, config: TradingConfig | None = None) -> None:
        self.config = config or TradingConfig()
        self._broker: PaperBroker | None = None
        self._portfolio_state: PortfolioState | None = None
        self._previous_net_worth: float = 0.0
        self._current_step: int = 0
        self._data_cache: dict[str, pd.DataFrame] = {}
        self._data_indices: dict[str, int] = {}
        self._episode_length: int = 0
        self._action_scheme: ActionScheme | None = None
        self._observer: Observer | None = None
        self._reward_scheme: RewardScheme | None = None
        self._action_space: gym.spaces.Space | None = None
        self._observation_space: gym.spaces.Space | None = None
        self._episode_reward: float = 0.0
        self._trade_count: int = 0
        self._buy_count: int = 0
        self._sell_count: int = 0
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        from trading_bot.rl.actions import BSHActionScheme, ProportionActionScheme
        from trading_bot.rl.observer import TensorTradeObserver

        if self.config.action_scheme == "proportion":
            self._action_scheme = ProportionActionScheme(symbols=self.config.symbols)
        else:
            self._action_scheme = BSHActionScheme(symbols=self.config.symbols)

        self._action_space = self._action_scheme.action_space

        reward_schemes: dict[str, RewardScheme] = {
            "simple_profit": SimpleProfitReward(),
            "risk_adjusted": RiskAdjustedReward(reward_scale=self.config.reward_scale),
            "compound_daily": CompoundDailyReward(),
            "shannon_entropy": ShannonEntropyReward(),
            "sharpe": SharpeReward(reward_scale=self.config.reward_scale),
            "drawdown_penalty": DrawdownPenaltyReward(reward_scale=self.config.reward_scale),
        }
        self._reward_scheme = reward_schemes.get(
            self.config.reward_scheme,
            reward_schemes["risk_adjusted"],
        )

        self._observer = TensorTradeObserver(
            symbols=self.config.symbols,
            window_size=self.config.observer_window,
            period=self.config.bar_period,
            interval=self.config.bar_interval,
            max_symbols=self.config.max_symbols,
        )
        self._observation_space = self._observer.observation_space

        self._initialized = True

    def _load_market_data(self, symbol: str) -> pd.DataFrame | None:
        if symbol in self._data_cache:
            return self._data_cache[symbol]

        from trading_bot.data.market_data import fetch_bars
        from trading_bot.rl.features import build_market_feature_frame

        try:
            df = fetch_bars(
                symbol, self.config.bar_period, self.config.bar_interval,
                end=self.config.data_end_date,
            )
            if df is not None and not df.empty and "close" in df.columns:
                self._data_cache[symbol] = build_market_feature_frame(df)
                self._data_indices[symbol] = 0
                return df
        except Exception as e:
            logger.warning(f"Failed to fetch market data for {symbol}: {e}")
        return None

    def _get_current_prices(self) -> dict[str, float]:
        prices: dict[str, float] = {}
        for symbol in self.config.symbols:
            df = self._load_market_data(symbol)
            if df is not None and not df.empty:
                idx = self._data_indices.get(symbol, 0)
                if 0 <= idx < len(df):
                    close = df.iloc[idx]["close"]
                    if pd.notna(close) and close > 0:
                        prices[symbol] = float(close)
        return prices

    def _advance_data(self, prices: dict[str, float]) -> bool:
        all_done = True
        for symbol in self.config.symbols:
            df = self._load_market_data(symbol)
            if df is not None and not df.empty:
                idx = self._data_indices.get(symbol, 0)
                if idx < len(df) - 1:
                    self._data_indices[symbol] = idx + 1
                    all_done = False
        return all_done

    def _compute_portfolio_value(self, prices: dict[str, float]) -> float:
        if self._broker is None:
            return self.config.starting_cash
        total = self._broker.cash
        for ticker, qty in self._broker.positions.items():
            if qty > 0 and ticker in prices:
                total += qty * prices[ticker]
        return total

    def _update_portfolio_state(self, prices: dict[str, float]) -> PortfolioState:
        if self._broker is None:
            return PortfolioState(cash=self.config.starting_cash, equity=self.config.starting_cash)

        positions: dict[str, Position] = {}
        for ticker, qty in self._broker.positions.items():
            if qty > 0:
                avg_cost = self._broker.position_costs.get(ticker, prices.get(ticker, 0.0))
                positions[ticker] = Position(
                    ticker=ticker,
                    quantity=qty,
                    average_cost=avg_cost,
                )

        market_value = sum(
            p.quantity * prices.get(p.ticker, p.average_cost)
            for p in positions.values()
        )
        equity = self._broker.cash + market_value
        unrealized_pnl = market_value - sum(p.quantity * p.average_cost for p in positions.values())

        return PortfolioState(
            cash=self._broker.cash,
            equity=equity,
            positions=positions,
            unrealized_pnl=unrealized_pnl,
        )

    def reset(
        self,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        super().reset(seed=seed)
        self._ensure_initialized()

        if seed is not None:
            self.np_random = np.random.RandomState(seed)

        self._broker = PaperBroker(
            starting_cash=self.config.starting_cash,
            fee_per_order=self.config.fee_per_order,
            slippage_bps=self.config.slippage_bps,
        )

        for symbol in self.config.symbols:
            self._load_market_data(symbol)

        start_idx = int(self._episode_length * self.config.random_start_pct)
        for symbol in self.config.symbols:
            df = self._data_cache.get(symbol)
            if df is not None and not df.empty:
                max_idx = max(0, len(df) - self.config.observer_window - 1)
                self._data_indices[symbol] = min(start_idx, max_idx)

        prices = self._get_current_prices()
        self._portfolio_state = self._update_portfolio_state(prices)
        self._previous_net_worth = self._portfolio_state.equity
        self._current_step = 0
        self._episode_reward = 0.0
        self._trade_count = 0
        self._buy_count = 0
        self._sell_count = 0
        if hasattr(self._reward_scheme, "reset"):
            self._reward_scheme.reset()

        obs = self._observer.observe(
            self._portfolio_state, prices, self._current_step,
            data_frames=self._data_cache, data_indices=self._data_indices,
        )
        self._episode_length += 1

        info: dict[str, Any] = {
            "step": self._current_step,
            "net_worth": self._portfolio_state.equity,
            "cash_ratio": self._portfolio_state.cash / max(self._portfolio_state.equity, 1e-8),
            "num_positions": len(self._portfolio_state.positions),
        }
        return obs, info

    def step(
        self, action: Any
    ) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        if self._action_scheme is None or self._observer is None:
            raise RuntimeError("Environment not initialized. Call reset() first.")

        self._action_scheme.reset_portfolio(self._broker)

        prices = self._get_current_prices()
        positions_before = dict(self._broker.positions) if self._broker else {}

        try:
            self._action_scheme.perform(action, prices)
        except ValueError as e:
            logger.debug(f"Action rejected: {e}")

        positions_after = dict(self._broker.positions) if self._broker else {}
        if positions_before != positions_after:
            self._trade_count += 1
            net_delta = sum(
                positions_after.get(symbol, 0) - positions_before.get(symbol, 0)
                for symbol in self.config.symbols
            )
            if net_delta > 0:
                self._buy_count += 1
            elif net_delta < 0:
                self._sell_count += 1

        self._portfolio_state = self._update_portfolio_state(prices)
        current_net_worth = self._portfolio_state.equity

        reward = self._reward_scheme.compute_reward(
            current_net_worth, self._previous_net_worth
        )
        self._episode_reward += reward

        self._previous_net_worth = current_net_worth
        self._current_step += 1

        all_done = self._advance_data(prices)
        truncated = (
            self._current_step >= self.config.max_episode_steps
            or all_done
        )
        terminated = False

        obs = self._observer.observe(
            self._portfolio_state, prices, self._current_step,
            data_frames=self._data_cache, data_indices=self._data_indices,
        )

        info: dict[str, Any] = {
            "step": self._current_step,
            "net_worth": current_net_worth,
            "cash_ratio": self._portfolio_state.cash / max(current_net_worth, 1e-8),
            "num_positions": len(self._portfolio_state.positions),
            "positions": dict(self._broker.positions) if self._broker else {},
            "reward": reward,
            "trade_count": self._trade_count,
        }

        if self._current_step > 0:
            total_return = (current_net_worth - self.config.starting_cash) / max(self.config.starting_cash, 1e-8)
            info["total_return_pct"] = total_return
        if terminated or truncated:
            info["episode_summary"] = asdict(self.get_episode_summary())

        return obs, reward, terminated, truncated, info

    @property
    def action_space(self) -> gym.spaces.Space:
        self._ensure_initialized()
        if self._action_space is None:
            raise RuntimeError("action_space not initialized")
        return self._action_space

    @property
    def observation_space(self) -> gym.spaces.Space:
        self._ensure_initialized()
        if self._observation_space is None:
            raise RuntimeError("observation_space not initialized")
        return self._observation_space

    def get_portfolio_state(self) -> PortfolioState | None:
        return self._portfolio_state

    def get_broker(self) -> PaperBroker | None:
        return self._broker

    def get_episode_summary(self) -> EpisodeSummary:
        ending_equity = (
            self._portfolio_state.equity
            if self._portfolio_state is not None
            else self.config.starting_cash
        )
        total_return_pct = (
            (ending_equity - self.config.starting_cash)
            / max(self.config.starting_cash, 1e-8)
        )
        open_positions = len(self._portfolio_state.positions) if self._portfolio_state else 0
        return EpisodeSummary(
            steps=self._current_step,
            starting_equity=self.config.starting_cash,
            ending_equity=ending_equity,
            total_reward=self._episode_reward,
            total_return_pct=total_return_pct,
            trade_count=self._trade_count,
            buy_count=self._buy_count,
            sell_count=self._sell_count,
            open_positions=open_positions,
        )

    def render(self, mode: str = None) -> None:
        if self._portfolio_state is None:
            return
        equity = self._portfolio_state.equity
        pnl = equity - self.config.starting_cash
        pnl_pct = (pnl / self.config.starting_cash) * 100
        positions = len(self._portfolio_state.positions)
        print(
            f"Step {self._current_step:4d} | "
            f"Equity: ${equity:12,.2f} | "
            f"P&L: ${pnl:10,.2f} ({pnl_pct:+.2f}%) | "
            f"Positions: {positions}"
        )

    def close(self) -> None:
        self._data_cache.clear()
        self._data_indices.clear()

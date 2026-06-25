from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from trading_bot.data.feature_engineering import FeatureEngineer
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.order import OrderRequest

if TYPE_CHECKING:
    import pandas as pd


class TradingEnv(gym.Env):
    """Gymnasium environment for RL-based stock trading.

    Wraps the existing PaperBroker and FeatureEngineer to provide a
    standard RL interface compatible with stable-baselines3 and other
    Gymnasium-compatible libraries.

    Action space (discrete):
        0: HOLD - No action
        1: BUY - Open long position (if no position)
        2: SELL - Close long position (if in position)

    State space:
        Normalized feature vector from FeatureEngineer (19 features standard, 24 extended)
        Plus 3 portfolio state features (position weight, unrealized PnL, cash ratio)

    Reward:
        Portfolio return change between steps, penalized for:
        - Transaction costs
        - Drawdown
        - Position turnover

    Usage::

        env = TradingEnv(daily_frame, ticker="AAPL", initial_cash=10000)
        obs, info = env.reset()
        while not done:
            action = agent.predict(obs)
            obs, reward, done, truncated, info = env.step(action)
    """

    metadata = {"render_modes": ["human"]}

    def __init__(
        self,
        daily_frame: "pd.DataFrame",
        ticker: str = "AAPL",
        initial_cash: float = 10000.0,
        feature_set: str = "standard",
        reward_scaling: float = 1.0,
        transaction_cost_bps: float = 10.0,
        render_mode: str | None = None,
        warmup_bars: int = 60,
    ) -> None:
        super().__init__()

        self.daily_frame = daily_frame
        self.ticker = ticker
        self.initial_cash = initial_cash
        self.feature_set = feature_set
        self.reward_scaling = reward_scaling
        self.transaction_cost_bps = transaction_cost_bps
        self.render_mode = render_mode
        self.warmup_bars = warmup_bars

        self.feature_engineer = FeatureEngineer(feature_set=feature_set)
        self.state_size = self.feature_engineer.state_size

        self.action_space = spaces.Discrete(3)
        self.observation_space = spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(self.state_size,),
            dtype=np.float64,
        )

        self._current_step = warmup_bars
        self._broker: PaperBroker | None = None
        self._position_shares = 0
        self._entry_price = 0.0
        self._peak_equity = initial_cash
        self._cumulative_reward = 0.0

    def reset(
        self,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        super().reset(seed=seed)

        self._current_step = self.warmup_bars
        self._broker = PaperBroker(
            starting_cash=self.initial_cash,
            fee_per_order=self.transaction_cost_bps / 10000.0 * self.initial_cash,
            slippage_bps=0,
        )
        self._position_shares = 0
        self._entry_price = 0.0
        self._peak_equity = self.initial_cash
        self._cumulative_reward = 0.0

        obs = self._build_observation()
        info = self._get_info()

        return obs, info

    def step(
        self, action: int
    ) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        current_price = self._get_current_price()
        prev_equity = self._get_portfolio_value(current_price)

        self._execute_action(action, current_price)

        self._current_step += 1
        current_price = self._get_current_price()
        current_equity = self._get_portfolio_value(current_price)

        reward = self._calculate_reward(prev_equity, current_equity, action)
        self._cumulative_reward += reward

        done = self._check_done()
        truncated = False

        obs = self._build_observation()
        info = self._get_info()

        if self.render_mode == "human":
            self._render_step(action, current_price, current_equity, reward)

        return obs, reward, done, truncated, info

    def _execute_action(self, action: int, price: float) -> None:
        if action == 0:
            return

        if action == 1 and self._position_shares == 0:
            shares = self._calculate_buy_shares(price)
            if shares > 0:
                order = OrderRequest(
                    ticker=self.ticker,
                    side="BUY",
                    order_type="market",
                    quantity=shares,
                    submitted_at=datetime.now(),
                )
                self._broker.submit_order(order, price)
                self._position_shares = shares
                self._entry_price = price

        elif action == 2 and self._position_shares > 0:
            order = OrderRequest(
                ticker=self.ticker,
                side="SELL",
                order_type="market",
                quantity=self._position_shares,
                submitted_at=datetime.now(),
            )
            self._broker.submit_order(order, price)
            self._position_shares = 0
            self._entry_price = 0.0

    def _calculate_buy_shares(self, price: float) -> int:
        if self._broker is None:
            return 0

        risk_per_share = price * 0.02
        dollar_risk = self._broker.cash * 0.01
        shares = int(dollar_risk / risk_per_share) if risk_per_share > 0 else 0

        max_shares = int((self._broker.cash * 0.20) / price)
        shares = min(shares, max_shares)

        return max(shares, 0)

    def _calculate_reward(
        self, prev_equity: float, current_equity: float, action: int
    ) -> float:
        if prev_equity <= 0:
            return 0.0

        return_pct = (current_equity - prev_equity) / prev_equity
        reward = return_pct * self.reward_scaling

        if action != 0:
            reward -= self.transaction_cost_bps / 10000.0

        max_drawdown = (self._peak_equity - current_equity) / self._peak_equity
        if max_drawdown > 0:
            reward -= max_drawdown * 0.5

        if current_equity > self._peak_equity:
            self._peak_equity = current_equity

        return float(reward)

    def _build_observation(self) -> np.ndarray:
        if self._broker is None:
            return np.zeros(self.state_size, dtype=np.float64)

        current_price = self._get_current_price()
        portfolio_value = self._get_portfolio_value(current_price)

        position_weight = (
            (self._position_shares * current_price) / portfolio_value
            if portfolio_value > 0
            else 0.0
        )

        unrealized_pnl_pct = (
            (current_price - self._entry_price) / self._entry_price
            if self._position_shares > 0 and self._entry_price > 0
            else 0.0
        )

        cash_ratio = self._broker.cash / portfolio_value if portfolio_value > 0 else 1.0

        window_start = max(0, self._current_step - 60)
        window_end = self._current_step + 1
        frame_window = self.daily_frame.iloc[window_start:window_end].copy()

        state = self.feature_engineer.build_state(
            frame=frame_window,
            ticker=self.ticker,
            portfolio_weight=position_weight,
            unrealized_pnl_pct=unrealized_pnl_pct,
            cash_ratio=cash_ratio,
        )

        # Replace NaN values with 0 to prevent neural network issues
        state = np.nan_to_num(state, nan=0.0, posinf=0.0, neginf=0.0)

        return state

    def _get_current_price(self) -> float:
        if self._current_step >= len(self.daily_frame):
            return float(self.daily_frame["close"].iloc[-1])
        return float(self.daily_frame["close"].iloc[self._current_step])

    def _get_portfolio_value(self, current_price: float) -> float:
        if self._broker is None:
            return self.initial_cash

        position_value = self._position_shares * current_price
        return self._broker.cash + position_value

    def _check_done(self) -> bool:
        if self._broker is None:
            return False

        if self._current_step >= len(self.daily_frame) - 1:
            return True

        if self._broker.cash <= 0:
            return True

        portfolio_value = self._get_portfolio_value(self._get_current_price())
        if portfolio_value < self.initial_cash * 0.5:
            return True

        return False

    def _get_info(self) -> dict[str, Any]:
        return {
            "step": self._current_step,
            "ticker": self.ticker,
            "cash": self._broker.cash if self._broker else self.initial_cash,
            "position_shares": self._position_shares,
            "entry_price": self._entry_price,
            "portfolio_value": self._get_portfolio_value(self._get_current_price()),
            "cumulative_reward": self._cumulative_reward,
        }

    def _render_step(
        self, action: int, price: float, equity: float, reward: float
    ) -> None:
        action_names = {0: "HOLD", 1: "BUY", 2: "SELL"}
        print(
            f"Step {self._current_step}: {action_names.get(action, 'UNKNOWN')} | "
            f"Price: ${price:.2f} | Equity: ${equity:.2f} | Reward: {reward:.4f}"
        )

    def render(self) -> None:
        if self.render_mode == "human":
            print(f"Episode finished. Final equity: ${self._get_portfolio_value(self._get_current_price()):.2f}")

from __future__ import annotations

from abc import ABC, abstractmethod

EPSILON = 1e-8


class RewardScheme(ABC):
    @abstractmethod
    def compute_reward(self, current_net_worth: float, previous_net_worth: float) -> float: ...


class SimpleProfitReward(RewardScheme):
    """Raw profit reward: change in net worth.

    reward = net_worth(t) - net_worth(t-1)

    Simple but unnormalized — magnitudes vary with account size.
    """

    def compute_reward(self, current_net_worth: float, previous_net_worth: float) -> float:
        return current_net_worth - previous_net_worth


class RiskAdjustedReward(RewardScheme):
    """Risk-adjusted return reward: proportional change in net worth.

    reward = ((net_worth(t) - net_worth(t-1)) / net_worth(t-1)) * REWARD_SCALE

    Normalized by previous net worth, scaled for better learning signals.
    """

    REWARD_SCALE = 1.0

    def __init__(self, reward_scale: float | None = None) -> None:
        self.reward_scale = reward_scale if reward_scale is not None else self.REWARD_SCALE

    def compute_reward(self, current_net_worth: float, previous_net_worth: float) -> float:
        if previous_net_worth < EPSILON:
            return 0.0
        return ((current_net_worth - previous_net_worth) / (previous_net_worth + EPSILON)) * self.reward_scale


class CompoundDailyReward(RewardScheme):
    """Compound return reward: log of net worth ratio.

    reward = log(net_worth(t) / net_worth(t-1))

    Equivalent to continuously compounded returns. Symmetric for
    gains and losses.
    """

    def compute_reward(self, current_net_worth: float, previous_net_worth: float) -> float:
        if previous_net_worth < EPSILON or current_net_worth < EPSILON:
            return 0.0
        return __import__("math").log(current_net_worth / (previous_net_worth + EPSILON))


class ShannonEntropyReward(RewardScheme):
    """Log return reward (Shannon entropy inspired).

    reward = log(net_worth(t) / net_worth(t-1))

    Same as CompoundDailyReward but named for RL literature compatibility.
    """

    def compute_reward(self, current_net_worth: float, previous_net_worth: float) -> float:
        if previous_net_worth < EPSILON or current_net_worth < EPSILON:
            return 0.0
        return __import__("math").log(current_net_worth / (previous_net_worth + EPSILON))


class SharpeReward(RewardScheme):
    """Approximate Sharpe ratio reward using a rolling window of returns.

    reward = mean(returns) / (std(returns) + epsilon) * scale

    Stateful: keeps a deque of recent per-step returns.  Call ``reset()``
    at the start of each episode.
    """

    def __init__(self, window: int = 20, reward_scale: float = 100.0) -> None:
        from collections import deque
        self.window = window
        self.reward_scale = reward_scale
        self._returns: deque[float] = deque(maxlen=window)

    def reset(self) -> None:
        self._returns.clear()

    def compute_reward(self, current_net_worth: float, previous_net_worth: float) -> float:
        if previous_net_worth < EPSILON:
            return 0.0
        ret = (current_net_worth - previous_net_worth) / (previous_net_worth + EPSILON)
        self._returns.append(ret)
        if len(self._returns) < 2:
            return ret * self.reward_scale
        import numpy as _np
        mean_ret = _np.mean(self._returns)
        std_ret = _np.std(self._returns)
        if std_ret < EPSILON:
            return mean_ret * self.reward_scale
        return (mean_ret / (std_ret + EPSILON)) * self.reward_scale


class DrawdownPenaltyReward(RewardScheme):
    """Risk-adjusted return penalised by drawdown from peak net worth.

    reward = pct_return * scale  -  drawdown * penalty_weight * scale

    Encourages the agent to avoid large drawdowns, not just chase returns.
    Stateful: tracks peak net worth across the episode.
    """

    def __init__(self, reward_scale: float = 100.0, penalty_weight: float = 2.0) -> None:
        self.reward_scale = reward_scale
        self.penalty_weight = penalty_weight
        self._peak: float = 0.0

    def reset(self) -> None:
        self._peak = 0.0

    def compute_reward(self, current_net_worth: float, previous_net_worth: float) -> float:
        if previous_net_worth < EPSILON:
            return 0.0
        if current_net_worth > self._peak:
            self._peak = current_net_worth
        pct_return = (current_net_worth - previous_net_worth) / (previous_net_worth + EPSILON)
        drawdown = (self._peak - current_net_worth) / (self._peak + EPSILON) if self._peak > 0 else 0.0
        return (pct_return - drawdown * self.penalty_weight) * self.reward_scale

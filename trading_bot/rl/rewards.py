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

    reward = (net_worth(t) - net_worth(t-1)) / net_worth(t-1)

    Normalized by previous net worth, making it comparable across
    different account sizes.
    """

    def compute_reward(self, current_net_worth: float, previous_net_worth: float) -> float:
        if previous_net_worth < EPSILON:
            return 0.0
        return (current_net_worth - previous_net_worth) / (previous_net_worth + EPSILON)


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

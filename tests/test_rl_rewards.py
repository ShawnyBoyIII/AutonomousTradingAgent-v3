"""Tests for RL reward schemes (130 lines)."""

from __future__ import annotations

import pytest

from trading_bot.rl.rewards import (
    CompoundDailyReward,
    DrawdownPenaltyReward,
    RiskAdjustedReward,
    SharpeReward,
    ShannonEntropyReward,
    SimpleProfitReward,
)


class TestSimpleProfitReward:
    def test_positive_profit(self):
        reward = SimpleProfitReward()
        result = reward.compute_reward(11000.0, 10000.0)
        assert result == 1000.0

    def test_negative_profit(self):
        reward = SimpleProfitReward()
        result = reward.compute_reward(9000.0, 10000.0)
        assert result == -1000.0

    def test_no_change(self):
        reward = SimpleProfitReward()
        result = reward.compute_reward(10000.0, 10000.0)
        assert result == 0.0

    def test_large_profit(self):
        reward = SimpleProfitReward()
        result = reward.compute_reward(100000.0, 50000.0)
        assert result == 50000.0


class TestRiskAdjustedReward:
    def test_positive_return(self):
        reward = RiskAdjustedReward()
        result = reward.compute_reward(11000.0, 10000.0)
        assert result > 0
        # Should be approximately (1000/10000) * 1.0 = 0.1
        assert abs(result - 0.1) < 0.001

    def test_negative_return(self):
        reward = RiskAdjustedReward()
        result = reward.compute_reward(9000.0, 10000.0)
        assert result < 0
        assert abs(result - (-0.1)) < 0.001

    def test_custom_scale(self):
        reward = RiskAdjustedReward(reward_scale=10.0)
        result = reward.compute_reward(11000.0, 10000.0)
        assert abs(result - 1.0) < 0.01

    def test_zero_previous_net_worth(self):
        reward = RiskAdjustedReward()
        result = reward.compute_reward(100.0, 0.0)
        assert result == 0.0

    def test_very_small_previous_net_worth(self):
        reward = RiskAdjustedReward()
        result = reward.compute_reward(100.0, 1e-9)
        # Should not crash, returns near-zero
        assert abs(result) < 1.0

    def test_no_change(self):
        reward = RiskAdjustedReward()
        result = reward.compute_reward(10000.0, 10000.0)
        assert result == 0.0


class TestCompoundDailyReward:
    def test_positive_return(self):
        reward = CompoundDailyReward()
        result = reward.compute_reward(11000.0, 10000.0)
        import math
        expected = math.log(11000.0 / 10000.0)
        assert abs(result - expected) < 0.001

    def test_negative_return(self):
        reward = CompoundDailyReward()
        result = reward.compute_reward(9000.0, 10000.0)
        import math
        expected = math.log(9000.0 / 10000.0)
        assert abs(result - expected) < 0.001

    def test_zero_previous_net_worth(self):
        reward = CompoundDailyReward()
        result = reward.compute_reward(100.0, 0.0)
        assert result == 0.0

    def test_zero_current_net_worth(self):
        reward = CompoundDailyReward()
        result = reward.compute_reward(0.0, 10000.0)
        assert result == 0.0

    def test_no_change(self):
        reward = CompoundDailyReward()
        result = reward.compute_reward(10000.0, 10000.0)
        import math
        assert abs(result - math.log(1.0)) < 0.001
        assert abs(result) < 0.001  # log(1.0) ≈ 0, allow floating point noise


class TestShannonEntropyReward:
    def test_same_as_compound_daily(self):
        reward1 = ShannonEntropyReward()
        reward2 = CompoundDailyReward()
        result1 = reward1.compute_reward(11000.0, 10000.0)
        result2 = reward2.compute_reward(11000.0, 10000.0)
        assert abs(result1 - result2) < 0.001

    def test_positive_return(self):
        reward = ShannonEntropyReward()
        result = reward.compute_reward(11000.0, 10000.0)
        assert result > 0

    def test_zero_previous_net_worth(self):
        reward = ShannonEntropyReward()
        result = reward.compute_reward(100.0, 0.0)
        assert result == 0.0


class TestSharpeReward:
    def test_single_return(self):
        reward = SharpeReward(window=20)
        reward.reset()
        result = reward.compute_reward(11000.0, 10000.0)
        # First return is just scaled raw return
        assert abs(result - 100.0 * 0.1) < 0.1

    def test_multiple_returns_sharpe(self):
        reward = SharpeReward(window=20)
        reward.reset()

        # Add 10 positive returns
        for i in range(10):
            reward.compute_reward(10000.0 + (i + 1) * 100, 10000.0 + i * 100)

        # After 2+ returns, should use Sharpe ratio
        result = reward.compute_reward(101000.0, 100900.0)
        assert result != 0.0

    def test_reset_clears_returns(self):
        reward = SharpeReward(window=20)
        reward.reset()
        reward.compute_reward(11000.0, 10000.0)
        reward.reset()

        # After reset, should treat as first return again
        result = reward.compute_reward(12000.0, 11000.0)
        assert abs(result - 100.0 * (1000.0 / 11000.0)) < 0.1

    def test_zero_previous_net_worth(self):
        reward = SharpeReward(window=20)
        reward.reset()
        result = reward.compute_reward(100.0, 0.0)
        assert result == 0.0

    def test_constant_returns_zero_sharpe(self):
        reward = SharpeReward(window=20)
        reward.reset()

        # Add returns with zero std (constant)
        for i in range(5):
            reward.compute_reward(10000.0 + (i + 1) * 100, 10000.0 + i * 100)

        # Should handle zero std gracefully
        result = reward.compute_reward(10500.0, 10400.0)
        assert result != 0.0

    def test_custom_window(self):
        reward = SharpeReward(window=10, reward_scale=50.0)
        assert reward.window == 10
        assert reward.reward_scale == 50.0

    def test_window_limit(self):
        reward = SharpeReward(window=5)
        reward.reset()

        # Add more returns than window size
        for i in range(10):
            reward.compute_reward(10000.0 + (i + 1) * 100, 10000.0 + i * 100)

        # Should only keep last 5 returns
        result = reward.compute_reward(11000.0, 10900.0)
        assert result != 0.0


class TestDrawdownPenaltyReward:
    def test_positive_return_no_drawdown(self):
        reward = DrawdownPenaltyReward()
        reward.reset()
        result = reward.compute_reward(11000.0, 10000.0)
        # pct_return = 0.1, drawdown = 0
        # reward = (0.1 - 0) * 100 = 10.0
        assert abs(result - 10.0) < 0.1

    def test_positive_return_with_drawdown(self):
        reward = DrawdownPenaltyReward()
        reward.reset()

        # First, set peak
        reward.compute_reward(12000.0, 10000.0)

        # Now drop from peak
        result = reward.compute_reward(11000.0, 12000.0)
        # pct_return = -1000/12000 = -0.0833
        # drawdown = (12000 - 11000) / 12000 = 0.0833
        # reward = (-0.0833 - 0.0833 * 2.0) * 100 = -25.0
        assert result < 0

    def test_negative_return(self):
        reward = DrawdownPenaltyReward()
        reward.reset()
        result = reward.compute_reward(9000.0, 10000.0)
        assert result < 0

    def test_reset_clears_peak(self):
        reward = DrawdownPenaltyReward()
        reward.reset()

        reward.compute_reward(12000.0, 10000.0)
        reward.reset()

        # After reset, peak is 0, so no drawdown penalty
        result = reward.compute_reward(11000.0, 10000.0)
        # pct_return = 0.1, drawdown = 0 (peak reset)
        # reward = 0.1 * 100 = 10.0
        assert abs(result - 10.0) < 0.1

    def test_custom_scale_and_penalty(self):
        reward = DrawdownPenaltyReward(reward_scale=50.0, penalty_weight=1.0)
        assert reward.reward_scale == 50.0
        assert reward.penalty_weight == 1.0

    def test_zero_previous_net_worth(self):
        reward = DrawdownPenaltyReward()
        reward.reset()
        result = reward.compute_reward(100.0, 0.0)
        assert result == 0.0

    def test_increasing_peak(self):
        reward = DrawdownPenaltyReward()
        reward.reset()

        # Build up peak
        reward.compute_reward(11000.0, 10000.0)
        reward.compute_reward(12000.0, 11000.0)

        # Now drawdown from peak of 12000
        result = reward.compute_reward(11500.0, 12000.0)
        assert result < 0  # Negative return + drawdown penalty

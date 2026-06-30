from __future__ import annotations

from trading_bot.config.settings import RLSettings
from trading_bot.rl.rewards import (
    PositionDurationBonus,
    WhipsawPenaltyReward,
)


class TestWhipsawPenaltyReward:
    def test_penalty_applied_after_large_loss_then_flat(self):
        reward = WhipsawPenaltyReward(window=5, penalty_scale=2.0)
        r1 = reward.compute_reward(10000.0, 10100.0)
        assert r1 < 0
        r2 = reward.compute_reward(10001.0, 10000.0)
        assert r2 < 0
        assert abs(r2) > abs(r1)

    def test_no_penalty_on_steady_gains(self):
        reward = WhipsawPenaltyReward(window=5, penalty_scale=2.0)
        r1 = reward.compute_reward(10050.0, 10000.0)
        r2 = reward.compute_reward(10100.0, 10050.0)
        assert r1 > 0
        assert r2 > 0

    def test_reset_clears_state(self):
        reward = WhipsawPenaltyReward(window=5)
        reward.compute_reward(9900.0, 10000.0)
        reward.reset()
        r = reward.compute_reward(10000.0, 10000.0)
        assert r == 0.0

    def test_zero_previous_net_worth(self):
        reward = WhipsawPenaltyReward()
        r = reward.compute_reward(10000.0, 0.0)
        assert r == 0.0


class TestPositionDurationBonus:
    def test_bonus_grows_with_consecutive_neutral_steps(self):
        reward = PositionDurationBonus(base_bonus=0.001)
        r1 = reward.compute_reward(10001.0, 10000.0)
        r2 = reward.compute_reward(10002.0, 10001.0)
        r3 = reward.compute_reward(10003.0, 10002.0)
        assert r3 > r1

    def test_bonus_resets_on_large_move(self):
        reward = PositionDurationBonus(base_bonus=0.001)
        reward.compute_reward(10001.0, 10000.0)
        reward.compute_reward(10002.0, 10001.0)
        r_after_big = reward.compute_reward(11000.0, 10002.0)
        r_after_neutral = reward.compute_reward(11001.0, 11000.0)
        assert r_after_neutral <= r_after_big or r_after_neutral < 0.01

    def test_reset_clears_state(self):
        reward = PositionDurationBonus(base_bonus=0.001)
        reward.compute_reward(10001.0, 10000.0)
        reward.compute_reward(10002.0, 10001.0)
        reward.compute_reward(10003.0, 10002.0)
        before = reward.compute_reward(10004.0, 10003.0)
        reward.reset()
        after = reward.compute_reward(10005.0, 10004.0)
        assert after < before

    def test_zero_previous_net_worth(self):
        reward = PositionDurationBonus()
        r = reward.compute_reward(10000.0, 0.0)
        assert r == 0.0


class TestRLSettingsDefaults:
    def test_model_paths_default_empty(self):
        assert RLSettings().model_paths == []

    def test_untrained_multiplier_default(self):
        assert RLSettings().untrained_confidence_threshold_multiplier == 0.8

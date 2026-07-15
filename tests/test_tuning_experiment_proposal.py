from trading_bot.learning.experiments.proposal import select_single_change


def test_select_single_change_picks_counter_veto_weight_first() -> None:
    baseline = {
        "supermodel": {"counter_veto_weight": 1.0, "block_threshold": 0.3},
        "strategy_tracker": {"full_allocation_rate": 0.5},
    }
    proposed = {
        "supermodel": {"counter_veto_weight": 0.5, "block_threshold": 0.2},
        "strategy_tracker": {"full_allocation_rate": 0.6},
    }

    change = select_single_change(baseline, proposed)

    assert change is not None
    assert (change.section, change.field) == ("supermodel", "counter_veto_weight")
    assert change.baseline == 1.0
    assert change.candidate == 0.75


def test_select_single_change_ignores_non_allowlisted_fields() -> None:
    baseline = {"risk": {"max_shares_per_position": 50}}
    proposed = {"risk": {"max_shares_per_position": 75}}

    assert select_single_change(baseline, proposed) is None


def test_select_single_change_returns_none_when_no_diff() -> None:
    baseline = {"supermodel": {"counter_veto_weight": 1.0}}
    proposed = {"supermodel": {"counter_veto_weight": 1.0}}

    assert select_single_change(baseline, proposed) is None

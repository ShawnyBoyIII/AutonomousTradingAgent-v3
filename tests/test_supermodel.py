from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

import pytest

from trading_bot.strategy.supermodel import (
    LayerVerdict,
    StackDecision,
    StackLayer,
    StackedSignal,
    _as_float,
    _as_int,
    _average,
    _clamp,
    _decision_from_layers,
    _verdict_from_score,
    build_stacked_signal,
)
from trading_bot.config.settings import SupermodelSettings


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@dataclass
class FakeSignal:
    confidence: float = 0.0


def _layer(name: str, score: float, verdict: LayerVerdict, reason: str = "r") -> StackLayer:
    return StackLayer(name=name, score=score, verdict=verdict, reason=reason)


VALID_LAYER_VERDICTS = set(get_args(LayerVerdict))
VALID_STACK_DECISIONS = set(get_args(StackDecision))


# ---------------------------------------------------------------------------
# build_stacked_signal — basic behaviour
# ---------------------------------------------------------------------------


def test_no_signal_returns_no_signal_decision() -> None:
    signal = build_stacked_signal("AAPL", None, {})
    assert signal.symbol == "AAPL"
    assert signal.score == 0.0
    assert signal.decision == "no_signal"
    assert len(signal.layers) == 1
    assert signal.layers[0].name == "setup"
    assert signal.layers[0].verdict == "neutral"


def test_no_signal_to_details_shape() -> None:
    signal = build_stacked_signal("SPY", None, {})
    details = signal.to_details()
    assert details["supermodel_decision"] == "no_signal"
    assert details["supermodel_score"] == 0.0
    assert isinstance(details["supermodel_layers"], str)


def test_no_signal_with_rl_sell_blocks() -> None:
    signal = build_stacked_signal("AAPL", None, {"rl_action": 2, "rl_confidence": 0.9})

    assert signal.decision == "block"
    assert any(layer.name == "rl" and layer.verdict == "block" for layer in signal.layers)


def test_no_signal_with_parallel_sell_consensus_blocks() -> None:
    signal = build_stacked_signal("AAPL", None, {"consensus": "SELL"})

    assert signal.decision == "block"
    assert any(layer.name == "consensus" and layer.verdict == "block" for layer in signal.layers)


def test_signal_only_has_setup_layer() -> None:
    signal = build_stacked_signal("AAPL", FakeSignal(confidence=0.8), {})
    assert len(signal.layers) == 1
    assert signal.layers[0].name == "setup"
    assert signal.layers[0].score == 0.8
    assert signal.layers[0].verdict == "support"


def test_score_is_average_of_layer_scores() -> None:
    signal = build_stacked_signal(
        "AAPL", FakeSignal(confidence=0.8), {"v3_total_score": 12.0}
    )
    # setup=0.8, v3 normalized=1.0 -> average=0.9
    assert signal.score == pytest.approx(0.9)
    assert signal.decision == "support"


# ---------------------------------------------------------------------------
# v3 layer
# ---------------------------------------------------------------------------


def test_v3_layer_normalized_by_twelve() -> None:
    signal = build_stacked_signal(
        "AAPL", FakeSignal(confidence=0.5), {"v3_total_score": 6.0}
    )
    v3 = [l for l in signal.layers if l.name == "v3"][0]
    assert v3.score == pytest.approx(0.5)
    assert v3.verdict == "caution"


def test_v3_score_clamped_to_one() -> None:
    signal = build_stacked_signal(
        "AAPL", FakeSignal(confidence=0.5), {"v3_total_score": 24.0}
    )
    v3 = [l for l in signal.layers if l.name == "v3"][0]
    assert v3.score == 1.0


def test_v3_layer_omitted_when_none() -> None:
    signal = build_stacked_signal("AAPL", FakeSignal(confidence=0.5), {})
    assert not any(l.name == "v3" for l in signal.layers)


# ---------------------------------------------------------------------------
# rl layer
# ---------------------------------------------------------------------------


def test_rl_buy_vote_creates_layer() -> None:
    signal = build_stacked_signal(
        "AAPL",
        FakeSignal(confidence=0.5),
        {"rl_action": 1, "rl_confidence": 0.9},
    )
    rl = [l for l in signal.layers if l.name == "rl"][0]
    assert rl.score == 0.9
    assert rl.verdict == "support"


def test_rl_sell_vote_blocks() -> None:
    signal = build_stacked_signal(
        "AAPL",
        FakeSignal(confidence=0.9),
        {"rl_action": 2, "rl_confidence": 0.9},
    )
    rl = [l for l in signal.layers if l.name == "rl"][0]
    assert rl.score == 0.0
    assert rl.verdict == "block"
    assert signal.decision == "block"


def test_rl_hold_vote_neutral() -> None:
    signal = build_stacked_signal(
        "AAPL", FakeSignal(confidence=0.5), {"rl_action": 0}
    )
    rl = [l for l in signal.layers if l.name == "rl"][0]
    assert rl.score == 0.5
    assert rl.verdict == "neutral"
    assert rl.reason == "RL hold vote"


def test_rl_layer_omitted_when_missing() -> None:
    signal = build_stacked_signal("AAPL", FakeSignal(confidence=0.5), {})
    assert not any(l.name == "rl" for l in signal.layers)


# ---------------------------------------------------------------------------
# swarm layer
# ---------------------------------------------------------------------------


def test_swarm_approve_creates_agent_layer() -> None:
    signal = build_stacked_signal(
        "AAPL",
        FakeSignal(confidence=0.8),
        {"swarm_decision": "APPROVE", "swarm_confidence": 0.75},
    )
    swarm = [l for l in signal.layers if l.name == "swarm"][0]
    assert swarm.score == 0.75
    assert swarm.verdict == "support"


def test_swarm_reject_is_caution_not_block() -> None:
    signal = build_stacked_signal(
        "AAPL",
        FakeSignal(confidence=0.9),
        {"swarm_decision": "REJECT", "swarm_confidence": 0.9},
    )
    swarm = [l for l in signal.layers if l.name == "swarm"][0]
    assert swarm.verdict == "caution"
    assert swarm.score == 0.35
    assert signal.decision != "block"


# ---------------------------------------------------------------------------
# counter-thesis layer
# ---------------------------------------------------------------------------


def test_counter_block_creates_block_layer() -> None:
    signal = build_stacked_signal(
        "AAPL", FakeSignal(confidence=0.9), {"counter_thesis_block": True}
    )
    counter = [l for l in signal.layers if l.name == "counter"][0]
    assert counter.verdict == "block"
    assert counter.score == 0.0
    assert signal.decision == "block"


def test_counter_confidence_creates_layer() -> None:
    signal = build_stacked_signal(
        "AAPL",
        FakeSignal(confidence=0.9),
        {"counter_thesis_confidence": 0.6},
    )
    counter = [l for l in signal.layers if l.name == "counter"][0]
    assert counter.score == 0.6
    assert counter.verdict == "caution"


def test_counter_layer_omitted_when_absent() -> None:
    signal = build_stacked_signal("AAPL", FakeSignal(confidence=0.5), {})
    assert not any(l.name == "counter" for l in signal.layers)


def test_counter_block_takes_precedence_over_confidence() -> None:
    signal = build_stacked_signal(
        "AAPL",
        FakeSignal(confidence=0.9),
        {"counter_thesis_block": True, "counter_thesis_confidence": 0.9},
    )
    counter = [l for l in signal.layers if l.name == "counter"][0]
    assert counter.verdict == "block"


def test_counter_block_can_be_softened_by_supermodel_settings() -> None:
    signal = build_stacked_signal(
        "AAPL",
        FakeSignal(confidence=0.9),
        {"counter_thesis_block": True},
        settings=SupermodelSettings(counter_veto_weight=0.0),
    )

    assert signal.decision == "support"
    assert signal.score == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# decision logic
# ---------------------------------------------------------------------------


def test_decision_block_when_any_layer_blocks() -> None:
    layers = [_layer("a", 0.9, "support"), _layer("b", 0.0, "block")]
    assert _decision_from_layers(0.9, layers) == "block"


def test_decision_support_at_threshold() -> None:
    layers = [_layer("a", 0.72, "support")]
    assert _decision_from_layers(0.72, layers) == "support"


def test_decision_thresholds_can_be_configured() -> None:
    layers = [_layer("a", 0.61, "caution")]
    settings = SupermodelSettings(support_threshold=0.6, block_threshold=0.2)
    assert _decision_from_layers(0.61, layers, settings=settings) == "support"


def test_decision_caution_at_threshold() -> None:
    layers = [_layer("a", 0.3, "caution")]
    assert _decision_from_layers(0.3, layers) == "caution"


def test_decision_block_when_score_below_threshold() -> None:
    layers = [_layer("a", 0.29, "block")]
    assert _decision_from_layers(0.29, layers) == "block"


# ---------------------------------------------------------------------------
# verdict helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score,expected",
    [
        (1.0, "support"),
        (0.72, "support"),
        (0.71, "caution"),
        (0.5, "caution"),
        (0.35, "caution"),
        (0.3, "caution"),
        (0.29, "block"),
        (0.0, "block"),
    ],
)
def test_verdict_from_score(score: float, expected: LayerVerdict) -> None:
    assert _verdict_from_score(score) == expected
    assert _verdict_from_score(score) in VALID_LAYER_VERDICTS


# ---------------------------------------------------------------------------
# numeric helpers
# ---------------------------------------------------------------------------


def test_clamp_within_range() -> None:
    assert _clamp(0.5) == 0.5


def test_clamp_below_zero() -> None:
    assert _clamp(-1.0) == 0.0


def test_clamp_above_one() -> None:
    assert _clamp(2.0) == 1.0


def test_average_empty() -> None:
    assert _average([]) == 0.0


def test_average_simple() -> None:
    assert _average([1.0, 2.0, 3.0]) == 2.0


def test_as_float_valid() -> None:
    assert _as_float("3.5", None) == 3.5


def test_as_float_invalid_returns_default() -> None:
    assert _as_float("not-a-number", 0.5) == 0.5


def test_as_float_none_returns_default() -> None:
    assert _as_float(None, 0.5) == 0.5


def test_as_int_valid() -> None:
    assert _as_int(1) == 1


def test_as_int_invalid_returns_none() -> None:
    assert _as_int("x") is None


# ---------------------------------------------------------------------------
# to_details + StackedSignal immutability
# ---------------------------------------------------------------------------


def test_to_details_contains_all_layers() -> None:
    signal = build_stacked_signal(
        "AAPL",
        FakeSignal(confidence=0.9),
        {"v3_total_score": 12.0, "rl_action": 1, "rl_confidence": 0.9},
    )
    details = signal.to_details()
    layers_str = details["supermodel_layers"]
    assert "setup:support" in layers_str
    assert "v3:support" in layers_str
    assert "rl:support" in layers_str
    assert details["supermodel_decision"] == signal.decision


def test_signal_is_frozen_dataclass() -> None:
    signal = build_stacked_signal("AAPL", None, {})
    with pytest.raises(Exception):
        signal.score = 1.0  # type: ignore[misc]


def test_sample_layer_is_frozen_dataclass() -> None:
    layer = _layer("setup", 0.5, "caution")
    with pytest.raises(Exception):
        layer.score = 1.0  # type: ignore[misc]


def test_decision_value_in_valid_set() -> None:
    signal = build_stacked_signal("AAPL", None, {})
    assert signal.decision in VALID_STACK_DECISIONS


def test_combined_layers_low_confidence_blocks() -> None:
    signal = build_stacked_signal(
        "AAPL",
        FakeSignal(confidence=0.3),
        {"v3_total_score": 2.0, "counter_thesis_confidence": 0.3},
    )
    # setup=0.3, v3=2/12=0.166, counter=0.3 -> avg ~ 0.255 -> block
    assert signal.decision == "block"
    assert signal.score < 0.3


def test_missing_signal_attributes_default_to_zero() -> None:
    @dataclass
    class EmptySignal:
        pass

    signal = build_stacked_signal("AAPL", EmptySignal(), {})
    setup = signal.layers[0]
    assert setup.score == 0.0
    assert setup.verdict == "block"

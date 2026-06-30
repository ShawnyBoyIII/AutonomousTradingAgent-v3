from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal


LayerVerdict = Literal["support", "caution", "block", "neutral"]
StackDecision = Literal["support", "caution", "block", "no_signal"]


@dataclass(frozen=True)
class StackLayer:
    name: str
    score: float
    verdict: LayerVerdict
    reason: str


@dataclass(frozen=True)
class StackedSignal:
    symbol: str
    score: float
    decision: StackDecision
    layers: tuple[StackLayer, ...]

    def to_details(self) -> dict[str, object]:
        return {
            "supermodel_score": round(self.score, 2),
            "supermodel_decision": self.decision,
            "supermodel_layers": ",".join(
                f"{layer.name}:{layer.verdict}:{layer.score:.2f}"
                for layer in self.layers
            ),
        }


def build_stacked_signal(
    symbol: str,
    signal: object | None,
    details: dict[str, object],
) -> StackedSignal:
    """Combine existing paper-mode evidence into one read-only advisory score."""
    layers: list[StackLayer] = []

    if signal is None:
        layers.append(StackLayer("setup", 0.0, "neutral", "no local trade signal"))
        return StackedSignal(symbol=symbol, score=0.0, decision="no_signal", layers=tuple(layers))

    confidence = _clamp(_as_float(getattr(signal, "confidence", None), 0.0))
    layers.append(
        StackLayer(
            "setup",
            confidence,
            _verdict_from_score(confidence),
            "local strategy confidence",
        )
    )

    v3_score = _as_float(details.get("v3_total_score"), None)
    if v3_score is not None:
        normalized = _clamp(v3_score / 12.0)
        layers.append(
            StackLayer(
                "v3",
                normalized,
                _verdict_from_score(normalized),
                "confluence/regime score",
            )
        )

    rl_action = _as_int(details.get("rl_action"))
    if rl_action is not None:
        rl_confidence = _clamp(_as_float(details.get("rl_confidence"), 0.5))
        if rl_action == 1:
            layers.append(StackLayer("rl", rl_confidence, _verdict_from_score(rl_confidence), "RL buy vote"))
        elif rl_action == 2:
            layers.append(StackLayer("rl", 0.0, "block", "RL sell vote"))
        else:
            layers.append(StackLayer("rl", 0.5, "neutral", "RL hold vote"))

    swarm_decision = str(details.get("swarm_decision", "")).upper()
    if swarm_decision:
        swarm_confidence = _clamp(_as_float(details.get("swarm_confidence"), 0.5))
        if swarm_decision == "APPROVE":
            layers.append(
                StackLayer("swarm", swarm_confidence, _verdict_from_score(swarm_confidence), "agent committee approve")
            )
        elif swarm_decision == "REJECT":
            layers.append(StackLayer("swarm", 0.0, "block", "agent committee reject"))
        else:
            layers.append(StackLayer("swarm", 0.5, "neutral", "agent committee hold"))

    if details.get("counter_thesis_block"):
        layers.append(StackLayer("counter", 0.0, "block", "counter-thesis blocked"))
    elif "counter_thesis_confidence" in details:
        counter_score = _clamp(_as_float(details.get("counter_thesis_confidence"), 1.0))
        layers.append(
            StackLayer(
                "counter",
                counter_score,
                _verdict_from_score(counter_score),
                "counter-thesis confidence multiplier",
            )
        )

    score = _average(layer.score for layer in layers)
    decision = _decision_from_layers(score, layers)
    return StackedSignal(symbol=symbol, score=score, decision=decision, layers=tuple(layers))


def _decision_from_layers(score: float, layers: list[StackLayer]) -> StackDecision:
    if any(layer.verdict == "block" for layer in layers):
        return "block"
    if score >= 0.72:
        return "support"
    if score >= 0.5:
        return "caution"
    return "block"


def _verdict_from_score(score: float) -> LayerVerdict:
    if score >= 0.72:
        return "support"
    if score >= 0.5:
        return "caution"
    return "block"


def _average(values: Iterable[float]) -> float:
    numeric = list(values)
    if not numeric:
        return 0.0
    return sum(numeric) / len(numeric)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _as_float(value: object, default: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

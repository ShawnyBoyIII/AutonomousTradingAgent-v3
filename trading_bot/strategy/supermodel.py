from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from trading_bot.config.settings import SupermodelSettings


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
    settings: SupermodelSettings | None = None,
) -> StackedSignal:
    """Combine existing paper-mode evidence into one read-only advisory score."""
    settings = settings or SupermodelSettings()
    layers: list[StackLayer] = []
    layer_weights: list[float] = []

    if signal is None:
        layers.append(StackLayer("setup", 0.0, "neutral", "no local trade signal"))
        layer_weights.append(1.0)
        has_blocking_evidence = (
            str(details.get("consensus", "")).upper() == "SELL"
            or _as_int(details.get("rl_action")) == 2
            or bool(details.get("counter_thesis_block"))
        )
        if not has_blocking_evidence:
            return StackedSignal(symbol=symbol, score=0.0, decision="no_signal", layers=tuple(layers))
    else:
        confidence = _clamp(_as_float(getattr(signal, "confidence", None), 0.0))
        layers.append(
            StackLayer(
                "setup",
                confidence,
                _verdict_from_score(confidence, settings=settings),
                "local strategy confidence",
            )
        )
        layer_weights.append(1.0)

    if str(details.get("consensus", "")).upper() == "SELL":
        layers.append(StackLayer("consensus", 0.0, "block", "parallel sell veto"))
        layer_weights.append(1.0)

    v3_score = _as_float(details.get("v3_total_score"), None)
    if v3_score is not None:
        normalized = _clamp(v3_score / 12.0)
        layers.append(
            StackLayer(
                "v3",
                normalized,
                _verdict_from_score(normalized, settings=settings),
                "confluence/regime score",
            )
        )
        layer_weights.append(1.0)

    rl_action = _as_int(details.get("rl_action"))
    if rl_action is not None:
        rl_confidence = _clamp(_as_float(details.get("rl_confidence"), 0.5))
        if rl_action == 1:
            layers.append(StackLayer("rl", rl_confidence, _verdict_from_score(rl_confidence, settings=settings), "RL buy vote"))
            layer_weights.append(1.0)
        elif rl_action == 2:
            layers.append(StackLayer("rl", 0.0, "block", "RL sell vote"))
            layer_weights.append(1.0)
        else:
            layers.append(StackLayer("rl", 0.5, "neutral", "RL hold vote"))
            layer_weights.append(1.0)

    if details.get("counter_thesis_block"):
        counter_weight = _clamp(settings.counter_veto_weight)
        counter_verdict: LayerVerdict = "block" if counter_weight >= 1.0 else "caution"
        layers.append(
            StackLayer(
                "counter",
                0.0 if counter_weight >= 1.0 else 0.5,
                counter_verdict,
                "counter-thesis blocked",
            )
        )
        layer_weights.append(counter_weight)
    elif "counter_thesis_confidence" in details:
        counter_score = _clamp(_as_float(details.get("counter_thesis_confidence"), 1.0))
        layers.append(
            StackLayer(
                "counter",
                counter_score,
                _verdict_from_score(counter_score, settings=settings),
                "counter-thesis confidence multiplier",
            )
        )
        layer_weights.append(1.0)

    score = _weighted_average(layers, layer_weights)
    decision = _decision_from_layers(score, layers, settings=settings)
    return StackedSignal(symbol=symbol, score=score, decision=decision, layers=tuple(layers))


def _decision_from_layers(
    score: float,
    layers: list[StackLayer],
    settings: SupermodelSettings | None = None,
) -> StackDecision:
    settings = settings or SupermodelSettings()
    if any(layer.verdict == "block" for layer in layers):
        return "block"
    if score >= settings.support_threshold:
        return "support"
    if score >= settings.block_threshold:
        return "caution"
    return "no_signal"


def _verdict_from_score(
    score: float,
    settings: SupermodelSettings | None = None,
) -> LayerVerdict:
    settings = settings or SupermodelSettings()
    if score >= settings.support_threshold:
        return "support"
    if score >= settings.block_threshold:
        return "caution"
    return "block"


def _average(values: Iterable[float]) -> float:
    numeric = list(values)
    if not numeric:
        return 0.0
    return sum(numeric) / len(numeric)


def _weighted_average(layers: Iterable[StackLayer], weights: Iterable[float]) -> float:
    numeric_layers = list(layers)
    numeric_weights = list(weights)
    if not numeric_layers:
        return 0.0
    total_weight = sum(numeric_weights)
    if total_weight <= 0.0:
        return _average(layer.score for layer in numeric_layers)
    return sum(layer.score * weight for layer, weight in zip(numeric_layers, numeric_weights, strict=True)) / total_weight


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

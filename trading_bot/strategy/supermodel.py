from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from trading_bot.config.settings import SupermodelSettings


LayerVerdict = Literal["support", "caution", "block", "neutral"]
StackDecision = Literal["support", "caution", "block", "no_signal"]


RANGE_BOUND_TREND_CAUTION_POLICY = "range_bound_trend_caution"


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


# ----------------------------------------------------------------------------
# V3-only conditional entry policy
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryPolicyDecision:
    """Result of applying a single tunable entry-size policy."""

    multiplier: float
    policy_name: str
    applied: bool
    reason: str
    metadata: dict[str, object] = field(default_factory=dict)


def select_source_metadata(public_votes: list[dict[str, object]]) -> dict[str, object]:
    """Extract the top-confidence BUY source metadata for downstream policy logic.

    Returns ``{"selected_source": ..., "selected_strategy": ...,
    "selected_confidence": float, "selected_action": str}``.

    V3 metadata (regime/strategy tags) is intentionally NOT consumed here —
    the predicate below keys only on the *selected* source so a V2.5 vote
    can never accidentally trigger the V3-only policy.
    """
    buy_votes = [
        vote
        for vote in public_votes
        if str(vote.get("action", "")).upper() == "BUY"
    ]
    if not buy_votes:
        return {
            "selected_source": None,
            "selected_strategy": None,
            "selected_confidence": 0.0,
            "selected_action": "HOLD",
        }
    best = max(buy_votes, key=lambda vote: float(vote.get("confidence", 0.0)))
    return {
        "selected_source": str(best.get("source", "")),
        "selected_strategy": str(best.get("strategy", "")),
        "selected_confidence": round(float(best.get("confidence", 0.0)), 6),
        "selected_action": "BUY",
    }


def compute_entry_policy_multiplier(
    *,
    selected_source: str | None,
    selected_strategy: str | None,
    regime: object | None,
    supermodel_decision: str | None,
    settings: SupermodelSettings | None = None,
) -> EntryPolicyDecision:
    """Pure helper that maps a fixed predicate to a tunable multiplier.

    Predicate (all four conditions required):
        selected_source == "v3"
        selected_strategy == "v3-trend_following"
        regime.value == "RANGE_BOUND"  (MarketRegime.RANGE_BOUND)
        supermodel_decision == "caution"

    Default multiplier is 1.0 (no scaling). A candidate value below 1.0 is
    activated ONLY by the validated experiment controller — direct config
    edits will be filtered by the loader's allowlist.
    """
    settings = settings or SupermodelSettings()

    regime_value = getattr(regime, "value", regime) if regime is not None else None

    conditions = {
        "selected_source_is_v3": selected_source == "v3",
        "selected_strategy_is_v3_trend_following": selected_strategy
        == "v3-trend_following",
        "regime_is_range_bound": regime_value == "range_bound",
        "supermodel_decision_is_caution": supermodel_decision == "caution",
    }
    matched = all(conditions.values())
    multiplier = float(settings.range_bound_trend_caution_multiplier)
    if matched:
        return EntryPolicyDecision(
            multiplier=multiplier,
            policy_name=RANGE_BOUND_TREND_CAUTION_POLICY,
            applied=multiplier != 1.0,
            reason=(
                f"{RANGE_BOUND_TREND_CAUTION_POLICY} matched; multiplier={multiplier:.2f}"
                if multiplier != 1.0
                else f"{RANGE_BOUND_TREND_CAUTION_POLICY} matched but multiplier=1.0 (no-op)"
            ),
            metadata={
                "selected_source": selected_source,
                "selected_strategy": selected_strategy,
                "regime": regime_value,
                "supermodel_decision": supermodel_decision,
                **conditions,
            },
        )
    return EntryPolicyDecision(
        multiplier=1.0,
        policy_name=RANGE_BOUND_TREND_CAUTION_POLICY,
        applied=False,
        reason=f"{RANGE_BOUND_TREND_CAUTION_POLICY} not matched; multiplier=1.0",
        metadata={
            "selected_source": selected_source,
            "selected_strategy": selected_strategy,
            "regime": regime_value,
            "supermodel_decision": supermodel_decision,
            **conditions,
        },
    )

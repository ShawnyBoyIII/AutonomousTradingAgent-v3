from __future__ import annotations

from trading_bot.learning.experiments.models import ParameterChange

PRIORITY = (
    ("supermodel", "counter_veto_weight"),
    ("supermodel", "block_threshold"),
    ("supermodel", "support_threshold"),
    ("strategy_tracker", "full_allocation_rate"),
)

STEP_RULES = {
    ("supermodel", "counter_veto_weight"): 0.25,
    ("supermodel", "block_threshold"): 0.05,
    ("supermodel", "support_threshold"): 0.05,
    ("strategy_tracker", "full_allocation_rate"): 0.05,
}


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


def select_single_change(
    baseline: dict[str, dict[str, float]],
    proposed: dict[str, dict[str, float]],
) -> ParameterChange | None:
    for section, field in PRIORITY:
        base_value = baseline.get(section, {}).get(field)
        proposed_value = proposed.get(section, {}).get(field)
        if base_value is None or proposed_value is None:
            continue
        if abs(float(proposed_value) - float(base_value)) < 1e-9:
            continue
        step = STEP_RULES[(section, field)]
        direction = 1 if proposed_value > base_value else -1
        candidate = float(base_value) + direction * step
        return ParameterChange(
            section=section,
            field=field,
            baseline=float(base_value),
            candidate=_clamp(candidate),
        )
    return None

"""Test for the MAX_DRAWDOWN_PCT env-var override.

2026-07-09 morning: after fixing Findings 1/3/2 the burn-in restarts
halt immediately on a spurious drawdown check (peak_dd=44.93% computed
from equity_history which contains the full pre-deposit and
post-deposit history). The check is hardcoded at line 966 of
auto-burn-in.sh:

    MAX_DRAWDOWN_PCT=10

This makes the env-var override `MAX_DRAWDOWN_PCT=50 ./scripts/auto-burn-in.sh`
a no-op. Fix: use the `${VAR:-default}` form so operators can override
the threshold without editing the script.

The TEMP override posture in AGENTS.md keeps the burn-in's
loose-guardrail config, so the smallest fix is the env-var override,
not changing the persisted config.
"""


def test_max_drawdown_pct_uses_env_override() -> None:
    """Line 966 must use ${MAX_DRAWDOWN_PCT:-10} so env var overrides the threshold."""
    from pathlib import Path

    script = Path(__file__).parent.parent / "scripts" / "auto-burn-in.sh"
    content = script.read_text(encoding="utf-8")
    # The hardcoded `MAX_DRAWDOWN_PCT=10` is the bug; the fix uses
    # `${MAX_DRAWDOWN_PCT:-10}` which bash expands to the env var or
    # the default 10 if unset.
    assert "MAX_DRAWDOWN_PCT=${MAX_DRAWDOWN_PCT:-10}" in content, (
        "auto-burn-in.sh must use ${MAX_DRAWDOWN_PCT:-10} so env-var "
        "override can temporarily raise the drawdown halt threshold"
    )
    # The hardcoded form must NOT exist anywhere (would override the env var)
    assert "MAX_DRAWDOWN_PCT=10\n" not in content, (
        "hardcoded MAX_DRAWDOWN_PCT=10 still present; env var would be ignored"
    )


def test_max_drawdown_pct_default_behavior(tmp_path) -> None:
    """Without env var, default 10 is used."""
    import subprocess
    import os

    env = os.environ.copy()
    env.pop("MAX_DRAWDOWN_PCT", None)
    proc = subprocess.Popen(
        ["bash", "-c", "MAX_DRAWDOWN_PCT=${MAX_DRAWDOWN_PCT:-10}; echo $MAX_DRAWDOWN_PCT"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout, _ = proc.communicate(timeout=5)
    assert stdout.decode().strip() == "10", f"default should be 10, got {stdout.decode()!r}"


def test_max_drawdown_pct_env_override_takes_effect(tmp_path) -> None:
    """With MAX_DRAWDOWN_PCT=50 set, the value is 50 (not 10)."""
    import subprocess
    import os

    env = os.environ.copy()
    env["MAX_DRAWDOWN_PCT"] = "50"
    proc = subprocess.Popen(
        ["bash", "-c", "MAX_DRAWDOWN_PCT=${MAX_DRAWDOWN_PCT:-10}; echo $MAX_DRAWDOWN_PCT"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout, _ = proc.communicate(timeout=5)
    assert stdout.decode().strip() == "50", f"override should be 50, got {stdout.decode()!r}"


def test_auto_burn_in_disables_drawdown_halt_when_config_disables_circuit_breaker() -> None:
    """Normal startup should honor burn-in-config.yaml's disabled drawdown guard."""
    from pathlib import Path

    script = (Path(__file__).parent.parent / "scripts" / "auto-burn-in.sh").read_text(encoding="utf-8")

    assert 'ENABLE_DRAWDOWN_CIRCUIT_BREAKER=$(' in script, (
        "auto-burn-in.sh must read enable_drawdown_circuit_breaker from the active config"
    )
    assert 'MAX_DRAWDOWN_PCT=${MAX_DRAWDOWN_PCT:-999}' in script, (
        "when burn-in-config disables the drawdown circuit breaker, the default shell threshold "
        "must not remain at 10%"
    )

"""Tests for advisory artifact gating and freshness rules.

These cover the invariant that an operator who disables advisory must not
have stale scout_override.yaml silently alter the burner universe.
"""
from __future__ import annotations

from pathlib import Path

from trading_bot.advisory.learner import (
    apply_scout_override,
    load_scout_override,
)
from trading_bot.config.settings import (
    AdvisorySettings,
    AppSettings,
    PaperSettings,
    Settings,
)


def _settings(enabled: bool, tmp_path: Path) -> Settings:
    advisory_dir = tmp_path / "advisory"
    advisory_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app=AppSettings(
            log_dir=str(tmp_path / "logs"),
            state_db_path=str(tmp_path / "state.db"),
            advisory_dir=str(advisory_dir),
        ),
        paper=PaperSettings(),
        advisory=AdvisorySettings(enabled=enabled),
    )


def test_load_scout_override_returns_empty_when_disabled(tmp_path: Path) -> None:
    """When advisory.enabled is False, load_scout_override must return {}
    regardless of whether scout_override.yaml exists on disk."""
    settings = _settings(enabled=False, tmp_path=tmp_path)
    artifact = Path(settings.app.advisory_dir) / "scout_override.yaml"
    artifact.write_text(
        "main_midcap:\n  promote_symbols: [AAPL, MSFT]\n  avoid_symbols: [TSLA]\n",
        encoding="utf-8",
    )
    assert load_scout_override(settings) == {}


def test_load_scout_override_returns_artifact_when_enabled(tmp_path: Path) -> None:
    settings = _settings(enabled=True, tmp_path=tmp_path)
    artifact = Path(settings.app.advisory_dir) / "scout_override.yaml"
    artifact.write_text(
        "main_midcap:\n  promote_symbols: [AAPL]\n  avoid_symbols: [TSLA]\n",
        encoding="utf-8",
    )
    loaded = load_scout_override(settings)
    assert loaded["main_midcap"]["promote_symbols"] == ["AAPL"]


def test_apply_scout_override_does_not_promote_when_disabled(tmp_path: Path) -> None:
    """Even with an artifact on disk, disabled advisory must not affect the universe."""
    settings = _settings(enabled=False, tmp_path=tmp_path)
    artifact = Path(settings.app.advisory_dir) / "scout_override.yaml"
    artifact.write_text(
        "main_midcap:\n  promote_symbols: [AAPL]\n  avoid_symbols: [TSLA]\n",
        encoding="utf-8",
    )
    merged = apply_scout_override(["SPY", "QQQ"], settings)
    assert merged == ["SPY", "QQQ"]

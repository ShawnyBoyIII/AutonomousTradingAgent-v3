from pathlib import Path

from trading_bot.config.loader import load_settings


def test_live_trading_remains_disabled_without_live_implementation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert settings.app.live_trading_enabled is False


def test_live_trading_remains_disabled_with_empty_default_config(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert settings.app.live_trading_enabled is False


def test_repo_default_config_keeps_rl_opt_in() -> None:
    settings = load_settings(Path("config.yaml"))

    assert settings.rl.enabled is False


def test_rl_guide_documents_default_rl_opt_in_policy() -> None:
    text = Path("docs/RL_TRADING_GUIDE.md").read_text(encoding="utf-8")

    assert "Root `config.yaml` keeps `rl.enabled: false`" in text
    assert "--model-path state/rl_logs/sector_diversity/PPO_seed_789.zip" in text

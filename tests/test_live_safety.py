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

from pathlib import Path

from trading_bot.config.loader import load_settings


def test_live_trading_remains_disabled_without_live_implementation(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        "  live_trading_enabled: true\n"
        "market_data:\n"
        "  provider: yfinance\n"
        "risk:\n"
        "  max_risk_per_trade_pct: 0.01\n"
        "  max_daily_risk_pct: 0.03\n"
        "  max_ticker_allocation_pct: 0.20\n"
        "  min_reward_risk_ratio: 2.0\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert settings.app.live_trading_enabled is False

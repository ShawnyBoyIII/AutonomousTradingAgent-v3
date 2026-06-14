from pathlib import Path

from trading_bot.config.loader import load_settings


def test_load_settings_reads_yaml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        "  live_trading_enabled: false\n"
        "  timezone: America/New_York\n"
        "  state_db_path: state/test.db\n"
        "  log_dir: logs\n"
        "market_data:\n"
        "  provider: yfinance\n"
        "  daily_period: 1y\n"
        "  intraday_period: 5d\n"
        "  intraday_interval: 5m\n"
        "risk:\n"
        "  max_risk_per_trade_pct: 0.01\n"
        "  max_daily_risk_pct: 0.03\n"
        "  max_ticker_allocation_pct: 0.20\n"
        "  min_reward_risk_ratio: 2.0\n",
        encoding="utf-8",
    )

    settings = load_settings(config_file)

    assert settings.app.live_trading_enabled is False
    assert settings.app.timezone == "America/New_York"
    assert settings.app.state_db_path == "state/test.db"
    assert settings.market_data.intraday_interval == "5m"
    assert settings.risk.max_daily_risk_pct == 0.03


def test_load_settings_forces_live_trading_off(tmp_path: Path) -> None:
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

    settings = load_settings(config_file)

    assert settings.app.live_trading_enabled is False

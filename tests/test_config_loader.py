from pathlib import Path

import pytest

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
    assert settings.app.state_db_path == str((tmp_path / "state/test.db").resolve())
    assert settings.app.scan_results_path == str((tmp_path / "state/scan_results.json").resolve())
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


@pytest.mark.parametrize(
    "risk_field",
    [
        "max_risk_per_trade_pct",
        "max_daily_risk_pct",
        "max_ticker_allocation_pct",
    ],
)
def test_load_settings_rejects_percentages_above_one(
    tmp_path: Path, risk_field: str
) -> None:
    config_file = tmp_path / "config.yaml"
    risk_settings = {
        "max_risk_per_trade_pct": "0.01",
        "max_daily_risk_pct": "0.03",
        "max_ticker_allocation_pct": "0.20",
        "min_reward_risk_ratio": "2.0",
    }
    risk_settings[risk_field] = "1.01"
    config_file.write_text(
        "app:\n"
        "  live_trading_enabled: false\n"
        "market_data:\n"
        "  provider: yfinance\n"
        "risk:\n"
        f"  max_risk_per_trade_pct: {risk_settings['max_risk_per_trade_pct']}\n"
        f"  max_daily_risk_pct: {risk_settings['max_daily_risk_pct']}\n"
        f"  max_ticker_allocation_pct: {risk_settings['max_ticker_allocation_pct']}\n"
        f"  min_reward_risk_ratio: {risk_settings['min_reward_risk_ratio']}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_settings(config_file)


def test_load_settings_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="top-level.*mapping"):
        load_settings(config_file)


def test_load_settings_resolves_relative_paths_from_config_directory(tmp_path: Path) -> None:
    config_dir = tmp_path / "nested"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "app:\n"
        "  state_db_path: state/local.db\n"
        "  log_dir: logs\n"
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

    assert settings.app.state_db_path == str((config_dir / "state/local.db").resolve())
    assert settings.app.log_dir == str((config_dir / "logs").resolve())
    assert settings.app.dashboard_summary_path == str(
        (config_dir / "state/dashboard_summary.json").resolve()
    )

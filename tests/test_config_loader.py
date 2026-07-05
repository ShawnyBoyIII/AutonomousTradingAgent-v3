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
    assert settings.app.universe_path == str((tmp_path / "state/universe.txt").resolve())
    assert settings.app.universe_candidates_path == str(
        (tmp_path / "state/universe_candidates.json").resolve()
    )
    assert settings.app.scan_results_path == str((tmp_path / "state/scan_results.json").resolve())
    assert settings.app.advisory_dir == str((tmp_path / "state/advisory_learner").resolve())
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


def test_load_settings_rejects_unknown_market_data_provider(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "market_data:\n"
        "  provider: alpacca\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported market data provider"):
        load_settings(config_file)


def test_load_settings_rejects_unknown_market_data_provider_stack(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "market_data:\n"
        "  providers:\n"
        "    - alpaca\n"
        "    - yahooo\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported market data provider"):
        load_settings(config_file)


def test_load_settings_normalizes_market_data_provider_names(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "market_data:\n"
        "  provider: ' Alpaca '\n"
        "  providers:\n"
        "    - ' Polygon '\n"
        "    - YFINANCE\n",
        encoding="utf-8",
    )

    settings = load_settings(config_file)

    assert settings.market_data.provider == "alpaca"
    assert settings.market_data.provider_stack == ["polygon", "yfinance"]


def test_load_settings_clamps_robinhood_mode_to_local_supported_values(
    monkeypatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("app:\n  state_db_path: state/test.db\n", encoding="utf-8")

    monkeypatch.setenv("ROBINHOOD_MODE", "live")

    settings = load_settings(config_file)

    assert settings.robinhood.mode == "shadow"


def test_load_settings_ignores_live_enable_flags_without_local_executor(
    monkeypatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("app:\n  state_db_path: state/test.db\n", encoding="utf-8")

    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "i_understand_the_risks")

    settings = load_settings(config_file)

    assert settings.app.live_trading_enabled is False


@pytest.mark.parametrize(
    "credential_line",
    [
        "  password: hunter2",
        "  mfa_secret: abc123",
        "  api_key: pk_live_123",
        "  api_secret: shh",
        "  device_token: device-123",
        "  token: bearer-token",
    ],
)
def test_load_settings_rejects_hardcoded_credential_like_values(
    tmp_path: Path, credential_line: str
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "robinhood:\n"
        f"{credential_line}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Credential detected"):
        load_settings(config_file)


@pytest.mark.parametrize(
    "credential_line",
    [
        "  api_key: ${APCA_API_KEY_ID}",
        "  api_secret: ${APCA_API_SECRET_KEY}",
        "  token: ${BROKER_TOKEN}",
    ],
)
def test_load_settings_allows_environment_credential_references(
    tmp_path: Path, credential_line: str
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "robinhood:\n"
        f"{credential_line}\n",
        encoding="utf-8",
    )

    load_settings(config_file)


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
        "  universe_path: state/universe.txt\n"
        "  universe_candidates_path: state/universe_candidates.json\n"
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
    assert settings.app.universe_path == str((config_dir / "state/universe.txt").resolve())
    assert settings.app.universe_candidates_path == str(
        (config_dir / "state/universe_candidates.json").resolve()
    )
    assert settings.app.log_dir == str((config_dir / "logs").resolve())
    assert settings.app.dashboard_summary_path == str(
        (config_dir / "state/dashboard_summary.json").resolve()
    )


def test_load_settings_resolves_sentiment_paths_from_config_directory(tmp_path: Path) -> None:
    config_dir = tmp_path / "nested"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "sentiment:\n"
        "  context_path: state/sentiment.json\n"
        "  memory_db_path: state/sentiment_memory.db\n",
        encoding="utf-8",
    )

    settings = load_settings(config_file)

    assert settings.sentiment.context_path == str((config_dir / "state/sentiment.json").resolve())
    assert settings.sentiment.memory_db_path == str(
        (config_dir / "state/sentiment_memory.db").resolve()
    )


def test_load_settings_applies_tuning_overrides_without_touching_structural_fields(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        "  live_trading_enabled: true\n"
        "market_data:\n"
        "  provider: yfinance\n"
        "risk:\n"
        "  max_ticker_allocation_pct: 0.15\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "tuning_overrides.yaml").write_text(
        "app:\n"
        "  live_trading_enabled: true\n"
        "risk:\n"
        "  max_ticker_allocation_pct: 0.99\n"
        "supermodel:\n"
        "  block_threshold: 0.22\n"
        "strategy_tracker:\n"
        "  min_win_rate: 0.3\n",
        encoding="utf-8",
    )

    settings = load_settings(config_file)

    assert settings.app.live_trading_enabled is False
    assert settings.risk.max_ticker_allocation_pct == 0.15
    assert settings.supermodel.block_threshold == 0.22
    assert settings.strategy_tracker.min_win_rate == 0.3

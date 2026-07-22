"""Configuration loader with environment variable support."""

import os
from pathlib import Path
from typing import Any

import yaml

from trading_bot.config.settings import Settings


def _resolve_relative_value(value: str, base_dir: Path) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        return str(candidate)
    return str((base_dir / candidate).resolve())


def _load_env_overrides(settings: Settings) -> None:
    """Load environment variable overrides for safety limits.

    Robinhood credentials (username/password/mfa_secret/device_token) are
    intentionally NOT loaded here: the MCP-only boundary never authenticates
    directly with Robinhood. All credentials live in the operator-managed MCP
    server. See RobinhoodSettings for the supported local knobs.
    """
    # Robinhood safety settings only (no credentials)
    if os.getenv("ROBINHOOD_MODE"):
        mode = os.getenv("ROBINHOOD_MODE", "shadow").lower()
        settings.robinhood.mode = mode if mode in {"shadow", "paper"} else "shadow"
    max_pos = os.getenv("ROBINHOOD_MAX_POSITION_VALUE")
    if max_pos and max_pos.strip():
        settings.robinhood.max_position_value = float(max_pos)
    daily_loss = os.getenv("ROBINHOOD_DAILY_LOSS_LIMIT")
    if daily_loss and daily_loss.strip():
        settings.robinhood.daily_loss_limit = float(daily_loss)
    dashboard_port = os.getenv("DASHBOARD_PORT")
    if dashboard_port and dashboard_port.strip():
        try:
            settings.app.dashboard_port = int(dashboard_port)
        except ValueError:
            pass


def _validate_credentials_not_in_config(config_text: str) -> None:
    """
    Validate that no credentials are hardcoded in config file.
    
    Raises ValueError if credentials detected.
    """
    sensitive_patterns = [
        "password:",
        "mfa_secret:",
        "api_secret:",
        "api_key:",
        "device_token:",
        "token:",
        "secret:",
    ]
    
    for line in config_text.split("\n"):
        line_lower = line.lower().strip()
        for pattern in sensitive_patterns:
            if pattern in line_lower:
                # Check if it has a value (not just the key)
                parts = line.split(":", 1)
                if len(parts) == 2:
                    value = parts[1].strip()
                    # If value is not empty and not a reference, it's a hardcoded credential
                    if value and not value.startswith("${") and value not in ["true", "false", "null", ""]:
                        raise ValueError(
                            f"Credential detected in config file: {line.strip()}\n"
                            "Credentials must be set via environment variables, not config files.\n"
                            "Use .env file (not committed to git) or actual environment variables."
                        )


def _load_live_trading_override(settings: Settings) -> None:
    """Keep local CLI out of live mode.

    ponytail: MCP boundary is shadow/read-only locally. If true live execution
    returns, reintroduce guarded env-based enabling with matching executor.
    """
    settings.app.live_trading_enabled = False


def _load_tuning_overrides(settings: Settings, base_dir: Path) -> None:
    override_path = Path(_resolve_relative_value(settings.app.tuning_overrides_path, base_dir))
    settings.app.tuning_overrides_path = str(override_path)
    if not override_path.exists():
        return

    try:
        config_text = override_path.read_text(encoding="utf-8")
    except OSError:
        return
    _validate_credentials_not_in_config(config_text)
    loaded = yaml.safe_load(config_text) or {}
    if not isinstance(loaded, dict):
        return

    allowed: dict[str, set[str]] = {
        "supermodel": {
            "support_threshold",
            "block_threshold",
            "counter_veto_weight",
            "range_bound_trend_caution_multiplier",
        },
        "strategy_tracker": {"window", "min_win_rate", "full_allocation_rate"},
    }

    for section_name, field_names in allowed.items():
        section_values = loaded.get(section_name)
        if not isinstance(section_values, dict):
            continue
        target = getattr(settings, section_name)
        for field_name in field_names:
            if field_name in section_values:
                setattr(target, field_name, section_values[field_name])


def load_settings(config_path: Path | None = None) -> Settings:
    """
    Load settings from YAML config file.
    
    Process:
    1. Load YAML config
    2. Validate no credentials in config file
    3. Override with environment variables
    4. Check for live trading enablement (env only)
    5. Resolve relative paths
    """
    path = config_path or Path("config.yaml")
    raw: dict[str, Any] = {}

    if path.exists():
        config_text = path.read_text(encoding="utf-8")
        
        # Validate no hardcoded credentials
        _validate_credentials_not_in_config(config_text)
        
        loaded = yaml.safe_load(config_text)
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError("Config YAML top-level must be a mapping")
        raw = loaded

    settings = Settings.model_validate(raw)
    base_dir = path.resolve().parent
    
    # Load environment overrides (credentials, etc.)
    _load_env_overrides(settings)
    _load_tuning_overrides(settings, base_dir)
    
    # CRITICAL SAFETY: Live trading only via environment + confirmation
    _load_live_trading_override(settings)

    settings.app.state_db_path = _resolve_relative_value(settings.app.state_db_path, base_dir)
    settings.app.universe_path = _resolve_relative_value(settings.app.universe_path, base_dir)
    settings.app.universe_candidates_path = _resolve_relative_value(
        settings.app.universe_candidates_path, base_dir
    )
    settings.app.watchlist_path = _resolve_relative_value(
        settings.app.watchlist_path, base_dir
    )
    settings.app.log_dir = _resolve_relative_value(settings.app.log_dir, base_dir)
    settings.app.dashboard_summary_path = _resolve_relative_value(
        settings.app.dashboard_summary_path, base_dir
    )
    settings.app.scan_results_path = _resolve_relative_value(
        settings.app.scan_results_path, base_dir
    )
    settings.app.approved_candidates_path = _resolve_relative_value(
        settings.app.approved_candidates_path, base_dir
    )
    settings.app.portfolio_summary_path = _resolve_relative_value(
        settings.app.portfolio_summary_path, base_dir
    )
    settings.app.backtest_summary_path = _resolve_relative_value(
        settings.app.backtest_summary_path, base_dir
    )
    settings.app.tuning_overrides_path = _resolve_relative_value(
        settings.app.tuning_overrides_path, base_dir
    )
    settings.app.advisory_dir = _resolve_relative_value(
        settings.app.advisory_dir, base_dir
    )
    settings.sentiment.context_path = _resolve_relative_value(
        settings.sentiment.context_path, base_dir
    )
    settings.sentiment.memory_db_path = _resolve_relative_value(
        settings.sentiment.memory_db_path, base_dir
    )
    return settings

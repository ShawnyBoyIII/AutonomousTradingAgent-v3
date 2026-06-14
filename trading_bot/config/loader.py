from pathlib import Path
from typing import Any

import yaml

from trading_bot.config.settings import Settings


def _resolve_relative_value(value: str, base_dir: Path) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        return str(candidate)
    return str((base_dir / candidate).resolve())


def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or Path("config.yaml")
    raw: dict[str, Any] = {}

    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Config YAML top-level must be a mapping")
        raw = loaded

    settings = Settings.model_validate(raw)
    settings.app.live_trading_enabled = False

    base_dir = path.resolve().parent
    settings.app.state_db_path = _resolve_relative_value(settings.app.state_db_path, base_dir)
    settings.app.log_dir = _resolve_relative_value(settings.app.log_dir, base_dir)
    return settings

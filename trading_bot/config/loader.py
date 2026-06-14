from pathlib import Path
from typing import Any

import yaml

from trading_bot.config.settings import Settings


def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or Path("config.yaml")
    raw: dict[str, Any] = {}

    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw = loaded if isinstance(loaded, dict) else {}

    settings = Settings.model_validate(raw)
    settings.app.live_trading_enabled = False
    return settings

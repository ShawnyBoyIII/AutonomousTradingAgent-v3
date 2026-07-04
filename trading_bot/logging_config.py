import logging
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s:%(lineno)d %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_HANDLER_MARKER = "_trading_bot_configured"

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return _LEVELS.get(level.upper(), logging.INFO)


def setup_logging(level: str | int = "INFO", log_file: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(_resolve_level(level))

    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            handler.close()
            root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    setattr(stream_handler, _HANDLER_MARKER, True)
    root.addHandler(stream_handler)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(path))
        file_handler.setFormatter(formatter)
        setattr(file_handler, _HANDLER_MARKER, True)
        root.addHandler(file_handler)


def configure_from_settings(settings) -> None:
    app_cfg = getattr(settings, "app", None)
    log_dir = getattr(app_cfg, "log_dir", "logs") if app_cfg is not None else "logs"
    log_level = getattr(app_cfg, "log_level", "INFO") if app_cfg is not None else "INFO"
    log_file = getattr(app_cfg, "log_file", None) if app_cfg is not None else None
    if log_file:
        resolved = Path(log_file)
        if not resolved.is_absolute() and log_dir:
            resolved = Path(log_dir) / resolved
        log_file = str(resolved)
    setup_logging(level=log_level, log_file=log_file)
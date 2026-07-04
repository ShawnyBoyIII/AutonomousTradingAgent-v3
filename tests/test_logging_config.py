import logging
from pathlib import Path

from trading_bot.logging_config import (
    _HANDLER_MARKER,
    configure_from_settings,
    setup_logging,
)


def _clear_root_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)


def test_setup_logging_sets_root_level() -> None:
    original = logging.getLogger().level
    try:
        setup_logging(level="DEBUG")
        assert logging.getLogger().level == logging.DEBUG
    finally:
        logging.getLogger().setLevel(original)


def test_setup_logging_is_idempotent_no_duplicate_handlers() -> None:
    _clear_root_handlers()
    try:
        setup_logging()
        first_count = len(logging.getLogger().handlers)
        setup_logging()
        setup_logging()
        second_count = len(logging.getLogger().handlers)
        assert first_count == second_count
        assert second_count == 1
    finally:
        _clear_root_handlers()


def test_setup_logging_default_level_info() -> None:
    _clear_root_handlers()
    try:
        setup_logging()
        assert logging.getLogger().level == logging.INFO
    finally:
        _clear_root_handlers()


def test_setup_logging_custom_level_debug() -> None:
    _clear_root_handlers()
    try:
        setup_logging(level="DEBUG")
        assert logging.getLogger().level == logging.DEBUG
    finally:
        _clear_root_handlers()


def test_setup_logging_with_file_creates_file_handler(tmp_path: Path) -> None:
    log_file = tmp_path / "out.log"
    _clear_root_handlers()
    try:
        setup_logging(log_file=str(log_file))
        handlers = logging.getLogger().handlers
        assert any(isinstance(h, logging.FileHandler) for h in handlers)
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "idempotent-mark", None, None
        )
        for h in handlers:
            if isinstance(h, logging.FileHandler):
                h.emit(record)
        h.close()
        assert log_file.exists()
        assert "idempotent-mark" in log_file.read_text(encoding="utf-8")
    finally:
        _clear_root_handlers()


def test_setup_logging_marks_handlers_for_removal() -> None:
    _clear_root_handlers()
    try:
        setup_logging()
        handlers = logging.getLogger().handlers
        assert all(getattr(h, _HANDLER_MARKER, False) for h in handlers)
    finally:
        _clear_root_handlers()


def test_setup_logging_accepts_int_level() -> None:
    _clear_root_handlers()
    try:
        setup_logging(level=logging.WARNING)
        assert logging.getLogger().level == logging.WARNING
    finally:
        _clear_root_handlers()


def test_configure_from_settings_uses_log_level(tmp_path: Path) -> None:
    from trading_bot.config.settings import Settings

    settings = Settings()
    settings.app.log_level = "WARNING"
    _clear_root_handlers()
    try:
        configure_from_settings(settings)
        assert logging.getLogger().level == logging.WARNING
    finally:
        _clear_root_handlers()


def test_configure_from_settings_resolves_log_file_relative_to_log_dir(
    tmp_path: Path,
) -> None:
    from trading_bot.config.settings import Settings

    settings = Settings()
    settings.app.log_dir = str(tmp_path)
    settings.app.log_file = "run.log"
    _clear_root_handlers()
    try:
        configure_from_settings(settings)
        handlers = logging.getLogger().handlers
        assert any(isinstance(h, logging.FileHandler) for h in handlers)
        for h in handlers:
            if isinstance(h, logging.FileHandler):
                assert Path(h.baseFilename).name == "run.log"
                assert tmp_path in Path(h.baseFilename).parents
                h.close()
    finally:
        _clear_root_handlers()
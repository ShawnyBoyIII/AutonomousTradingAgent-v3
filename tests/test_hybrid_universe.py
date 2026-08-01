from trading_bot.runtime.universe import merge_universe_symbols
from trading_bot.config.settings import ScoutSettings
from trading_bot.config.loader import load_settings


def test_scout_settings_expose_hybrid_universe_controls() -> None:
    settings = ScoutSettings(
        static_core_path="config/burn-in-core-symbols.txt",
        min_universe_size=10,
        preserve_previous_on_underflow=True,
    )

    assert settings.static_core_path == "config/burn-in-core-symbols.txt"
    assert settings.min_universe_size == 10
    assert settings.preserve_previous_on_underflow is True


def test_loader_resolves_static_core_path_relative_to_config(tmp_path) -> None:
    config_file = tmp_path / "burn-in.yaml"
    config_file.write_text(
        "scout:\n  static_core_path: core.txt\n",
        encoding="utf-8",
    )

    settings = load_settings(config_file)

    assert settings.scout.static_core_path == str((tmp_path / "core.txt").resolve())


def test_merge_universe_symbols_prioritizes_core_and_watchlist() -> None:
    symbols, preserved = merge_universe_symbols(
        static_symbols=["spy", "QQQ", "SPY"],
        watchlist_symbols=["AAPL", "qqq"],
        scout_symbols=["NVDA", "MSFT"],
        previous_symbols=["IWM"],
        max_size=10,
        min_size=3,
    )

    assert symbols == ["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "IWM"]
    assert preserved is False


def test_merge_universe_symbols_preserves_previous_when_refresh_is_too_small() -> None:
    symbols, preserved = merge_universe_symbols(
        static_symbols=[],
        watchlist_symbols=["AAPL"],
        scout_symbols=[],
        previous_symbols=["MSFT", "NVDA", "IWM"],
        max_size=10,
        min_size=3,
    )

    assert symbols == ["AAPL", "MSFT", "NVDA", "IWM"]
    assert preserved is True


def test_merge_universe_symbols_caps_output_after_deduplication() -> None:
    symbols, preserved = merge_universe_symbols(
        static_symbols=["SPY", "QQQ"],
        watchlist_symbols=["AAPL"],
        scout_symbols=["MSFT", "NVDA", "IWM"],
        previous_symbols=["DIA"],
        max_size=4,
        min_size=2,
    )

    assert symbols == ["SPY", "QQQ", "AAPL", "MSFT"]
    assert preserved is False

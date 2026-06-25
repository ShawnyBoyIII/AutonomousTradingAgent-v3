from trading_bot.config.settings import ScoutSettings
from trading_bot.scout import build_scout_candidates


def test_build_scout_candidates_merges_duplicates_and_ranks_stably() -> None:
    settings = ScoutSettings(max_universe_size=10, max_snapshot_candidates=10)
    rows = [
        {
            "symbol": "DUPE",
            "quoteType": "EQUITY",
            "exchange": "NYQ",
            "marketCap": 500_000_000,
            "regularMarketPrice": 10.0,
            "averageDailyVolume3Month": 200_000,
            "dayVolume": 500_000,
            "source": "aggressive_small_caps",
        },
        {
            "symbol": "DUPE",
            "quoteType": "EQUITY",
            "exchange": "NYQ",
            "marketCap": 500_000_000,
            "regularMarketPrice": 10.0,
            "averageDailyVolume3Month": 200_000,
            "dayVolume": 400_000,
            "source": "small_cap_gainers",
        },
        {
            "symbol": "SLOW",
            "quoteType": "EQUITY",
            "exchange": "NYQ",
            "marketCap": 1_500_000_000,
            "regularMarketPrice": 8.0,
            "averageDailyVolume3Month": 150_000,
            "dayVolume": 120_000,
            "source": "aggressive_small_caps",
        },
    ]

    result = build_scout_candidates(rows, settings)

    assert result["summary"]["candidates"] == 2
    assert result["summary"]["included"] == 2
    assert [row["ticker"] for row in result["candidates"]] == ["DUPE", "SLOW"]
    assert result["included_symbols"] == ["DUPE", "SLOW"]

    first = result["candidates"][0]
    assert first["source_hits"] == 2
    assert first["source_names"] == ["aggressive_small_caps", "small_cap_gainers"]
    assert first["included"] is True
    assert first["rank"] == 1
    assert first["volume_ratio"] == 2.5
    assert first["scout_score"] > result["candidates"][1]["scout_score"]


def test_build_scout_candidates_excludes_bad_filters() -> None:
    settings = ScoutSettings(max_universe_size=10, max_snapshot_candidates=10)
    rows = [
        {
            "symbol": "ETF1",
            "quoteType": "ETF",
            "exchange": "NYQ",
            "marketCap": 400_000_000,
            "regularMarketPrice": 22.0,
            "averageDailyVolume3Month": 300_000,
            "source": "small_cap_gainers",
        },
        {
            "symbol": "OTCC",
            "quoteType": "EQUITY",
            "exchange": "OTC",
            "marketCap": 400_000_000,
            "regularMarketPrice": 22.0,
            "averageDailyVolume3Month": 300_000,
            "source": "small_cap_gainers",
        },
        {
            "symbol": "CHEAP",
            "quoteType": "EQUITY",
            "exchange": "NYQ",
            "marketCap": 300_000_000,
            "regularMarketPrice": 1.5,
            "averageDailyVolume3Month": 500_000,
            "source": "small_cap_gainers",
        },
    ]

    result = build_scout_candidates(rows, settings)

    assert result["summary"]["included"] == 0
    assert result["included_symbols"] == []
    assert all(row["included"] is False for row in result["candidates"])


def test_build_scout_candidates_does_not_mix_best_fields_across_rows() -> None:
    settings = ScoutSettings(max_universe_size=10, max_snapshot_candidates=10)
    rows = [
        {
            "symbol": "MIXD",
            "quoteType": "EQUITY",
            "exchange": "NYQ",
            "marketCap": 300_000_000,
            "regularMarketPrice": 1.0,
            "averageDailyVolume3Month": 500_000,
            "source": "aggressive_small_caps",
        },
        {
            "symbol": "MIXD",
            "quoteType": "EQUITY",
            "exchange": "NYQ",
            "marketCap": 300_000_000,
            "regularMarketPrice": 10.0,
            "averageDailyVolume3Month": 10_000,
            "source": "small_cap_gainers",
        },
    ]

    result = build_scout_candidates(rows, settings)

    assert result["summary"]["included"] == 0
    assert result["candidates"][0]["included"] is False

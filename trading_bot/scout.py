from __future__ import annotations

from math import isfinite

from trading_bot.config.settings import ScoutSettings


def build_scout_candidates(
    rows: list[dict[str, object]],
    settings: ScoutSettings,
) -> dict[str, object]:
    grouped: dict[str, list[dict[str, object]]] = {}
    errors = 0
    for row in rows:
        if not isinstance(row, dict):
            errors += 1
            continue
        ticker = _ticker(row)
        if not ticker:
            errors += 1
            continue
        grouped.setdefault(ticker, []).append(row)

    candidates = [
        _build_candidate(ticker, grouped_rows, settings)
        for ticker, grouped_rows in grouped.items()
    ]
    ranked = sorted(
        candidates,
        key=lambda row: (
            not bool(row["included"]),
            -float(row["scout_score"]),
            str(row["ticker"]),
        ),
    )

    included_rank = 1
    included_count = 0
    included_symbols: list[str] = []
    for row in ranked:
        if row["included"] and included_count < settings.max_universe_size:
            row["rank"] = included_rank
            included_rank += 1
            included_count += 1
            included_symbols.append(str(row["ticker"]))
            continue
        if row["included"]:
            row["included"] = False
            row["reasons"].append("outside top universe limit")
        row["rank"] = None

    ranked.sort(
        key=lambda row: (
            not bool(row["included"]),
            row["rank"] if row["rank"] is not None else 999999,
            -float(row["scout_score"]),
            str(row["ticker"]),
        )
    )

    return {
        "candidates": ranked,
        "included_symbols": included_symbols,
        "summary": {
            "candidates": len(ranked),
            "included": len(included_symbols),
            "excluded": len(ranked) - len(included_symbols),
            "errors": errors,
        },
    }


def _build_candidate(
    ticker: str,
    rows: list[dict[str, object]],
    settings: ScoutSettings,
) -> dict[str, object]:
    source_names: list[str] = []
    for row in rows:
        source = str(row.get("source", "")).strip()
        if source and source not in source_names:
            source_names.append(source)

    primary = _select_primary_row(rows, settings)
    quote_type = str(primary.get("quoteType", "EQUITY")).strip().upper()
    exchange = str(primary.get("exchange", "")).strip().upper()
    market_cap = _max_float([primary], "marketCap", "intradaymarketcap")
    price = _max_float([primary], "regularMarketPrice", "intradayprice", "price")
    avg_volume = _max_float(
        [primary],
        "averageDailyVolume3Month",
        "avgdailyvol3m",
        "regularMarketVolume",
    )
    day_volume = _max_float([primary], "dayVolume", "dayvolume", "regularMarketVolume")
    avg_dollar_volume = (
        round((price or 0.0) * (avg_volume or 0.0), 2)
        if price is not None and avg_volume is not None
        else 0.0
    )
    volume_ratio = None
    if avg_volume is not None and avg_volume > 0 and day_volume is not None:
        volume_ratio = round(day_volume / avg_volume, 2)

    reasons: list[str] = []
    included = True
    if quote_type not in {"EQUITY", "COMMON_STOCK"}:
        included = False
        reasons.append("non-equity instrument")
    if "OTC" in exchange:
        included = False
        reasons.append("otc listing")
    if (
        market_cap is None
        or market_cap < settings.min_market_cap
        or market_cap > settings.max_market_cap
    ):
        included = False
        reasons.append("market cap outside band")
    if price is None or price < settings.min_price:
        included = False
        reasons.append("price below minimum")
    if avg_dollar_volume < settings.min_avg_dollar_volume:
        included = False
        reasons.append("liquidity below minimum")
    if included:
        reasons.append("passed scout filters")
        if len(source_names) > 1:
            reasons.append(f"appeared in {len(source_names)} screeners")
        if volume_ratio is not None:
            reasons.append(f"volume ratio {volume_ratio:.2f}")

    scout_score = round(
        (
            0.40
            * _liquidity_score(avg_dollar_volume, settings.min_avg_dollar_volume)
            + 0.35 * _momentum_score(source_names, volume_ratio)
            + 0.25
            * _size_score(
                market_cap,
                settings.min_market_cap,
                settings.max_market_cap,
            )
        )
        * 100.0,
        2,
    )

    return {
        "ticker": ticker,
        "scout_score": scout_score,
        "rank": None,
        "included": included,
        "source_hits": len(source_names),
        "source_names": source_names,
        "market_cap": int(market_cap) if market_cap is not None else None,
        "price": round(price, 2) if price is not None else None,
        "avg_dollar_volume": avg_dollar_volume,
        "volume_ratio": volume_ratio,
        "reasons": reasons,
    }


def _select_primary_row(
    rows: list[dict[str, object]],
    settings: ScoutSettings,
) -> dict[str, object]:
    def row_rank(row: dict[str, object]) -> tuple[int, float, float]:
        price = _max_float([row], "regularMarketPrice", "intradayprice", "price")
        avg_volume = _max_float(
            [row],
            "averageDailyVolume3Month",
            "avgdailyvol3m",
            "regularMarketVolume",
        )
        avg_dollar_volume = (
            (price or 0.0) * (avg_volume or 0.0)
            if price is not None and avg_volume is not None
            else 0.0
        )
        return (
            1 if _row_passes_filters(row, settings) else 0,
            avg_dollar_volume,
            price or 0.0,
        )

    return max(rows, key=row_rank)


def _row_passes_filters(row: dict[str, object], settings: ScoutSettings) -> bool:
    quote_type = str(row.get("quoteType", "EQUITY")).strip().upper()
    exchange = str(row.get("exchange", "")).strip().upper()
    market_cap = _max_float([row], "marketCap", "intradaymarketcap")
    price = _max_float([row], "regularMarketPrice", "intradayprice", "price")
    avg_volume = _max_float(
        [row],
        "averageDailyVolume3Month",
        "avgdailyvol3m",
        "regularMarketVolume",
    )
    avg_dollar_volume = (
        (price or 0.0) * (avg_volume or 0.0)
        if price is not None and avg_volume is not None
        else 0.0
    )
    return (
        quote_type in {"EQUITY", "COMMON_STOCK"}
        and "OTC" not in exchange
        and market_cap is not None
        and settings.min_market_cap <= market_cap <= settings.max_market_cap
        and price is not None
        and price >= settings.min_price
        and avg_dollar_volume >= settings.min_avg_dollar_volume
    )


def _ticker(row: dict[str, object]) -> str:
    for key in ("symbol", "ticker"):
        value = str(row.get(key, "")).upper().strip()
        if value:
            return value
    return ""


def _first_text(rows: list[dict[str, object]], key: str, default: str = "") -> str:
    for row in rows:
        value = str(row.get(key, "")).strip()
        if value:
            return value
    return default


def _max_float(rows: list[dict[str, object]], *keys: str) -> float | None:
    values: list[float] = []
    for row in rows:
        for key in keys:
            numeric = _to_float(row.get(key))
            if numeric is not None:
                values.append(numeric)
    return max(values) if values else None


def _to_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if isfinite(numeric) else None


def _clamp(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _liquidity_score(avg_dollar_volume: float, min_avg_dollar_volume: float) -> float:
    if min_avg_dollar_volume <= 0:
        return 1.0
    return _clamp(avg_dollar_volume / (min_avg_dollar_volume * 5.0))


def _momentum_score(source_names: list[str], volume_ratio: float | None) -> float:
    source_score = 0.55
    if "small_cap_gainers" in source_names:
        source_score = 0.65
    if len(source_names) > 1:
        source_score = 1.0
    volume_score = _clamp((volume_ratio or 0.0) / 2.0)
    return max(source_score, volume_score)


def _size_score(
    market_cap: float | None,
    min_market_cap: float,
    max_market_cap: float,
) -> float:
    if market_cap is None:
        return 0.0
    band = max_market_cap - min_market_cap
    if band <= 0:
        return 1.0
    return _clamp(1.0 - ((market_cap - min_market_cap) / band))

"""Offline-first sentiment/news context loading.

The trading app never depends on live news fetches for core signal generation.
Operators can write a JSON context file, and optional RSS fetching can enrich it
when explicitly enabled in config.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from xml.etree import ElementTree

from trading_bot.config.settings import Settings

logger = logging.getLogger(__name__)


def load_sentiment_context(settings: Settings, symbols: list[str]) -> dict[str, Any]:
    """Load local sentiment context and optionally enrich it from RSS feeds."""
    if not settings.sentiment.enabled:
        return {}

    normalized_symbols = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
    context = _load_local_context(Path(settings.sentiment.context_path))
    context.setdefault("source", "sentiment_context")
    context["tickers"] = _normalize_ticker_contexts(context.get("tickers"), normalized_symbols)

    if settings.sentiment.fetch_rss and settings.sentiment.rss_feeds:
        rss_items = _fetch_rss_items(
            settings.sentiment.rss_feeds,
            max_items_per_feed=settings.sentiment.max_items_per_feed,
        )
        _merge_rss_items(context, normalized_symbols, rss_items)

    return _filter_context_symbols(context, normalized_symbols)


def _load_local_context(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("sentiment context load failed path=%s error=%s", path, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("sentiment context ignored: top-level JSON must be an object")
        return {}
    return raw


def _normalize_ticker_contexts(value: object, symbols: list[str]) -> dict[str, dict[str, Any]]:
    tickers: dict[str, dict[str, Any]] = {}
    if isinstance(value, dict):
        for ticker, raw_context in value.items():
            symbol = str(ticker).strip().upper()
            if not symbol:
                continue
            tickers[symbol] = raw_context if isinstance(raw_context, dict) else {}

    for symbol in symbols:
        tickers.setdefault(symbol, {})
    return tickers


def _fetch_rss_items(feeds: list[str], max_items_per_feed: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for feed_url in feeds:
        try:
            with urlopen(feed_url, timeout=5) as response:  # noqa: S310 - operator-provided feeds.
                payload = response.read()
        except Exception as exc:
            logger.warning("rss sentiment fetch failed url=%s error=%s", feed_url, exc)
            continue
        items.extend(_parse_rss_payload(payload, feed_url, max_items_per_feed))
    return items


def _parse_rss_payload(payload: bytes, feed_url: str, max_items: int) -> list[dict[str, Any]]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        logger.warning("rss sentiment parse failed url=%s error=%s", feed_url, exc)
        return []

    parsed: list[dict[str, Any]] = []
    for item in root.findall(".//item")[:max_items]:
        title = _child_text(item, "title")
        description = _child_text(item, "description")
        link = _child_text(item, "link")
        if not title and not description:
            continue
        parsed.append(
            {
                "title": title or description[:120],
                "description": description,
                "url": link,
                "source": feed_url,
            }
        )
    return parsed


def _child_text(item: ElementTree.Element, child_name: str) -> str:
    child = item.find(child_name)
    return (child.text or "").strip() if child is not None else ""


def _merge_rss_items(
    context: dict[str, Any],
    symbols: list[str],
    rss_items: list[dict[str, Any]],
) -> None:
    tickers = context.setdefault("tickers", {})
    if not isinstance(tickers, dict):
        tickers = {}
        context["tickers"] = tickers

    for item in rss_items:
        text = f"{item.get('title', '')} {item.get('description', '')}".upper()
        matched_symbols = [symbol for symbol in symbols if symbol in text]
        for symbol in matched_symbols:
            ticker_context = tickers.setdefault(symbol, {})
            news = ticker_context.setdefault("news", [])
            if isinstance(news, list):
                news.append({**item, "source": item.get("source", "rss")})
            ticker_context.setdefault("source", "rss")


def _filter_context_symbols(context: dict[str, Any], symbols: list[str]) -> dict[str, Any]:
    if not symbols:
        return context
    tickers = context.get("tickers")
    if isinstance(tickers, dict):
        context["tickers"] = {
            symbol: ticker_context
            for symbol, ticker_context in tickers.items()
            if symbol.upper() in symbols
        }
    return context

from __future__ import annotations

import json
from pathlib import Path

from trading_bot.config.settings import Settings, SentimentSettings
from trading_bot.sentiment.context import load_sentiment_context


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def test_load_sentiment_context_returns_empty_when_disabled(tmp_path: Path) -> None:
    settings = Settings(
        sentiment=SentimentSettings(
            enabled=False,
            context_path=str(tmp_path / "missing.json"),
        )
    )

    assert load_sentiment_context(settings, ["AAPL"]) == {}


def test_load_sentiment_context_reads_local_json_and_filters_symbols(tmp_path: Path) -> None:
    context_path = tmp_path / "sentiment.json"
    context_path.write_text(
        json.dumps(
            {
                "vix": 13.5,
                "breadth": {"advance_decline_ratio": 1.9},
                "tickers": {
                    "aapl": {"news": [{"title": "AAPL raises guidance", "sentiment": 0.7}]},
                    "MSFT": {"news": [{"title": "MSFT downgrade", "sentiment": -0.5}]},
                },
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(sentiment=SentimentSettings(context_path=str(context_path)))

    context = load_sentiment_context(settings, ["AAPL"])

    assert context["vix"] == 13.5
    assert context["breadth"] == {"advance_decline_ratio": 1.9}
    assert sorted(context["tickers"]) == ["AAPL"]
    assert context["tickers"]["AAPL"]["news"][0]["sentiment"] == 0.7


def test_load_sentiment_context_missing_file_creates_symbol_slots(tmp_path: Path) -> None:
    settings = Settings(
        sentiment=SentimentSettings(context_path=str(tmp_path / "missing.json"))
    )

    context = load_sentiment_context(settings, ["AAPL", "MSFT"])

    assert sorted(context["tickers"]) == ["AAPL", "MSFT"]
    assert context["tickers"]["AAPL"] == {}


def test_load_sentiment_context_enriches_matching_symbols_from_rss(
    monkeypatch,
    tmp_path: Path,
) -> None:
    rss_payload = b"""
    <rss><channel>
      <item>
        <title>AAPL upgraded after strong demand</title>
        <description>Analysts raise price targets.</description>
        <link>https://example.test/aapl</link>
      </item>
      <item>
        <title>Unrelated market recap</title>
        <description>No ticker match.</description>
      </item>
    </channel></rss>
    """

    def fake_urlopen(url: str, timeout: int = 5) -> _FakeResponse:
        assert url == "https://example.test/rss"
        assert timeout == 5
        return _FakeResponse(rss_payload)

    monkeypatch.setattr("trading_bot.sentiment.context.urlopen", fake_urlopen)
    settings = Settings(
        sentiment=SentimentSettings(
            context_path=str(tmp_path / "missing.json"),
            fetch_rss=True,
            rss_feeds=["https://example.test/rss"],
        )
    )

    context = load_sentiment_context(settings, ["AAPL", "MSFT"])

    assert len(context["tickers"]["AAPL"]["news"]) == 1
    assert context["tickers"]["AAPL"]["news"][0]["title"].startswith("AAPL upgraded")
    assert context["tickers"]["MSFT"] == {}

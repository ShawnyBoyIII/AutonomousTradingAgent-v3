from __future__ import annotations

import requests

from trading_bot.data.providers.finnhub_provider import _redact_query_secrets as redact_finnhub
from trading_bot.data.providers.polygon_provider import _redact_query_secrets as redact_polygon


def test_provider_error_redaction_hides_query_secrets() -> None:
    message = requests.exceptions.ConnectionError(
        "https://api.example.test/path?apiKey=abc123&token=def456"
    )

    redacted = redact_polygon(message)
    redacted += " " + redact_finnhub(message)

    assert "abc123" not in redacted
    assert "def456" not in redacted
    assert "apiKey=<redacted>" in redacted
    assert "token=<redacted>" in redacted

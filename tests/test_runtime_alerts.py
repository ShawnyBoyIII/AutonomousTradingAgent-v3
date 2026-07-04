"""Tests for runtime.alerts module (31 lines)."""

from __future__ import annotations

from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from trading_bot.runtime import alerts


class _FakeResponse:
    def __init__(self) -> None:
        self._buf = BytesIO(b"ok")

    def read(self) -> bytes:
        return self._buf.read()


class TestSendDiscordMessage:
    def test_empty_webhook_returns_false(self) -> None:
        assert alerts.send_discord_message(webhook_url="", content="hi", username="bot") is False
        assert alerts.send_discord_message(webhook_url="   ", content="hi", username="bot") is False

    def test_success_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout):  # noqa: ANN001
            captured["url"] = req.full_url
            captured["data"] = req.data
            captured["method"] = req.method
            captured["timeout"] = timeout
            return _FakeResponse()

        monkeypatch.setattr(alerts.request, "urlopen", fake_urlopen)
        result = alerts.send_discord_message(
            webhook_url="https://discord.example/hook",
            content="hello world",
            username="tradebot",
        )
        assert result is True
        assert captured["url"] == "https://discord.example/hook"
        assert captured["method"] == "POST"
        assert captured["timeout"] == 10
        assert b"hello world" in captured["data"]
        assert b"tradebot" in captured["data"]

    def test_http_error_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(req, timeout):  # noqa: ANN001
            raise HTTPError(url=req.full_url, code=400, msg="Bad Request", hdrs=None, fp=None)

        monkeypatch.setattr(alerts.request, "urlopen", fake_urlopen)
        assert alerts.send_discord_message(
            webhook_url="https://discord.example/hook", content="x", username="b"
        ) is False

    def test_url_error_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(req, timeout):  # noqa: ANN001
            raise URLError("no connection")

        monkeypatch.setattr(alerts.request, "urlopen", fake_urlopen)
        assert alerts.send_discord_message(
            webhook_url="https://discord.example/hook", content="x", username="b"
        ) is False

    def test_timeout_error_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(req, timeout):  # noqa: ANN001
            raise TimeoutError("timed out")

        monkeypatch.setattr(alerts.request, "urlopen", fake_urlopen)
        assert alerts.send_discord_message(
            webhook_url="https://discord.example/hook", content="x", username="b"
        ) is False

    def test_content_type_header_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout):  # noqa: ANN001
            captured["headers"] = list(req.header_items())
            return _FakeResponse()

        monkeypatch.setattr(alerts.request, "urlopen", fake_urlopen)
        alerts.send_discord_message(
            webhook_url="https://discord.example/hook", content="x", username="b"
        )
        # header_items uses capitalization: Content-type
        assert any(k.lower() == "content-type" and v == "application/json" for k, v in captured["headers"])
"""Tests for the EOD fetcher (AWS Sig v4 + S3 GET + CSV filter)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from trading_bot.data.eod_fetcher import (
    MassiveFlatFilesClient,
    build_s3_key,
    filter_csv_to_universe,
    parse_massive_day_aggregates_csv,
    sign_aws_sig_v4,
)


# ---------------------------------------------------------------------------
# AWS Signature v4
#
# These vectors are taken from the official AWS test suite
# (https://docs.aws.amazon.com/general/latest/gr/sigv4-signed-request-examples.html).
# Using canonical, well-known inputs means a regression here is a clear bug,
# not a test-harness drift problem.
# ---------------------------------------------------------------------------


class TestAwsSigV4:
    def test_signs_known_request_to_expected_signature(self) -> None:
        # AWS reference example "get-vanilla" from the official Examples of
        # the complete Version 4 signing process documentation:
        # https://docs.aws.amazon.com/general/latest/gr/sigv4-create-canonical-request.html
        # Inputs are reproduced exactly below; the expected signature is the
        # value AWS publishes for these exact inputs.
        signature = sign_aws_sig_v4(
            method="GET",
            canonical_uri="/",
            canonical_querystring="Action=ListUsers&Version=2010-05-08",
            canonical_headers=(
                "content-type:application/x-www-form-urlencoded; charset=utf-8\n"
                "host:iam.amazonaws.com\n"
                "x-amz-date:20150830T123600Z\n"
            ),
            signed_headers="content-type;host;x-amz-date",
            payload_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            access_key="AKIDEXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            region="us-east-1",
            service="iam",
            amz_date="20150830T123600Z",
        )
        assert signature == (
            "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
        )


# ---------------------------------------------------------------------------
# S3 key construction
# ---------------------------------------------------------------------------


class TestBuildS3Key:
    def test_day_aggregates_key(self) -> None:
        assert (
            build_s3_key("day-aggregates", date(2026, 7, 7))
            == "stocks/day-aggregates/2026-07-07.csv"
        )

    def test_minute_aggregates_key(self) -> None:
        assert (
            build_s3_key("minute-aggregates", date(2026, 7, 7))
            == "stocks/minute-aggregates/2026-07-07.csv.gz"
        )

    def test_custom_template_with_year_subdir(self) -> None:
        """Massive.com's hosted bucket uses year-suffixed paths."""
        tmpl = "us_stocks_sip/day_aggs_v1/{year}/{date}.csv.gz"
        assert (
            build_s3_key("day-aggregates", date(2026, 7, 7), key_template=tmpl)
            == "us_stocks_sip/day_aggs_v1/2026/2026-07-07.csv.gz"
        )

    def test_custom_template_supports_all_placeholders(self) -> None:
        tmpl = "{product}/{year}/{month}/{day}.csv"
        assert (
            build_s3_key("day-aggregates", date(2026, 7, 7), key_template=tmpl)
            == "day-aggregates/2026/07/07.csv"
        )


# ---------------------------------------------------------------------------
# CSV parsing — Massive.com schema
# ---------------------------------------------------------------------------


class TestParseCsv:
    def test_parses_minimal_csv_string(self) -> None:
        csv_text = (
            "ticker,volume,open,close,high,low,window_start,transactions\n"
            "AAPL,100,100.0,101.0,102.0,99.0,1680033600000000000,50\n"
        )
        df = parse_massive_day_aggregates_csv(csv_text)
        assert len(df) == 1
        row = df.iloc[0]
        assert row["ticker"] == "AAPL"
        assert row["close"] == 101.0
        assert row["window_start"] == 1_680_033_600_000_000_000


# ---------------------------------------------------------------------------
# Universe filtering — keep only rows for our symbols
# ---------------------------------------------------------------------------


class TestUniverseFilter:
    def test_filters_to_universe_symbols(self) -> None:
        df = pd.DataFrame(
            {
                "ticker": ["AAPL", "NVDA", "FOO", "BAR", "AAPL"],
                "close": [1, 2, 3, 4, 5],
            }
        )
        universe = {"AAPL", "NVDA"}
        filtered = filter_csv_to_universe(df, universe)
        assert sorted(filtered["ticker"].unique()) == ["AAPL", "NVDA"]
        assert len(filtered) == 3  # 2 AAPL rows + 1 NVDA row

    def test_filter_is_case_insensitive(self) -> None:
        df = pd.DataFrame({"ticker": ["aapl", "NvDa"], "close": [1, 2]})
        universe = {"AAPL", "NVDA"}
        filtered = filter_csv_to_universe(df, universe)
        assert len(filtered) == 2

    def test_empty_universe_returns_empty(self) -> None:
        df = pd.DataFrame({"ticker": ["AAPL", "NVDA"], "close": [1, 2]})
        filtered = filter_csv_to_universe(df, set())
        assert filtered.empty


# ---------------------------------------------------------------------------
# MassiveFlatFilesClient — end-to-end with mocked HTTP
# ---------------------------------------------------------------------------


def _fake_csv() -> str:
    return (
        "ticker,volume,open,close,high,low,window_start,transactions\n"
        "AAPL,100,100.0,101.0,102.0,99.0,1680033600000000000,50\n"
        "NVDA,200,200.0,201.0,202.0,199.0,1680033600000000000,60\n"
        "FOO,300,300.0,301.0,302.0,299.0,1680033600000000000,70\n"
    )


class TestS3ClientGet:
    def test_get_returns_text_body(self) -> None:
        client = MassiveFlatFilesClient(
            endpoint="https://example.s3.endpoint",
            access_key="AKID-test",
            secret_key="test-secret",
            bucket="test-bucket",
            region="us-east-1",
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = _fake_csv().encode("utf-8")
        with patch("trading_bot.data.eod_fetcher.requests.get", return_value=mock_response):
            body = client.get_object_text("stocks/day-aggregates/2026-07-07.csv")
        assert "AAPL" in body

    def test_get_decompresses_gzip_body(self) -> None:
        """Minute aggregates come back as .csv.gz; auto-decompress."""
        import gzip

        client = MassiveFlatFilesClient(
            endpoint="https://example.s3.endpoint",
            access_key="AKID-test",
            secret_key="test-secret",
            bucket="test-bucket",
            region="us-east-1",
        )
        csv_bytes = _fake_csv().encode("utf-8")
        gz_bytes = gzip.compress(csv_bytes)
        # Sanity: the gzipped bytes start with the gzip magic 1f 8b.
        assert gz_bytes[:2] == b"\x1f\x8b"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = gz_bytes
        with patch("trading_bot.data.eod_fetcher.requests.get", return_value=mock_response):
            body = client.get_object_text("stocks/minute-aggregates/2026-07-07.csv.gz")
        assert "AAPL" in body
        assert "NVDA" in body

    def test_no_throttle_sleep_on_first_attempt(self) -> None:
        """First request should be fast; throttle only fires after a 5xx."""
        client = MassiveFlatFilesClient(
            endpoint="https://example.s3.endpoint",
            access_key="AKID-test",
            secret_key="test-secret",
            bucket="test-bucket",
            region="us-east-1",
            throttle_seconds=0.2,
        )
        ok = MagicMock(status_code=200, content=b"hello")
        with patch(
            "trading_bot.data.eod_fetcher.requests.get", return_value=ok
        ) as mock_get, patch("trading_bot.data.eod_fetcher.time.sleep") as mock_sleep:
            client.get_object_text("stocks/day-aggregates/2026-07-07.csv")
        assert mock_get.call_count == 1
        assert mock_sleep.call_count == 0, (
            "first attempt must not sleep; throttle only after a failed try"
        )

    def test_throttle_sleeps_after_5xx(self) -> None:
        client = MassiveFlatFilesClient(
            endpoint="https://example.s3.endpoint",
            access_key="AKID-test",
            secret_key="test-secret",
            bucket="test-bucket",
            region="us-east-1",
            throttle_seconds=0.2,
        )
        fail = MagicMock(status_code=503, content=b"Service Unavailable")
        ok = MagicMock(status_code=200, content=b"hello")
        with patch(
            "trading_bot.data.eod_fetcher.requests.get",
            side_effect=[fail, ok],
        ), patch("trading_bot.data.eod_fetcher.time.sleep") as mock_sleep:
            client.get_object_text("stocks/day-aggregates/2026-07-07.csv")
        # First attempt: no sleep. Retry: sleep once at the base throttle.
        assert mock_sleep.call_count == 1
        assert mock_sleep.call_args.args[0] == 0.2

    def test_path_style_addressing(self) -> None:
        """Path-style endpoints must not prepend bucket to host."""
        client = MassiveFlatFilesClient(
            endpoint="https://files.example.com",
            access_key="AKID-test",
            secret_key="test-secret",
            bucket="flatfiles",
            region="us-east-1",
            addressing_style="path",
        )
        url, canonical_uri = client._url_for("stocks/day-aggregates/2026-07-07.csv")
        assert url == "https://files.example.com/flatfiles/stocks/day-aggregates/2026-07-07.csv"
        assert canonical_uri == "/flatfiles/stocks/day-aggregates/2026-07-07.csv"

    def test_rejects_unknown_addressing_style(self) -> None:
        import pytest
        with pytest.raises(ValueError, match="addressing_style"):
            MassiveFlatFilesClient(
                endpoint="https://example.com",
                access_key="AKID-test",
                secret_key="test-secret",
                bucket="flatfiles",
                addressing_style="gopher",
            )

    def test_verify_tls_defaults_to_true(self) -> None:
        """Default cert verification is on (secure by default)."""
        from unittest.mock import MagicMock, patch
        mock = MagicMock(status_code=200, content=b"ok")
        with patch("trading_bot.data.eod_fetcher.requests.get", return_value=mock) as mock_get:
            client = MassiveFlatFilesClient(
                endpoint="https://example.com",
                access_key="AKID-test", secret_key="test-secret",
                bucket="b",
            )
            client.get_object_text("k")
        assert mock_get.call_args.kwargs["verify"] is True

    def test_verify_tls_can_be_disabled_for_self_signed_endpoints(self) -> None:
        """Operator can opt into insecure mode for trusted self-signed endpoints."""
        from unittest.mock import MagicMock, patch
        mock = MagicMock(status_code=200, content=b"ok")
        with patch("trading_bot.data.eod_fetcher.requests.get", return_value=mock) as mock_get:
            client = MassiveFlatFilesClient(
                endpoint="https://example.com",
                access_key="AKID-test", secret_key="test-secret",
                bucket="b", verify_tls=False,
            )
            client.get_object_text("k")
        assert mock_get.call_args.kwargs["verify"] is False

    def test_tls_ca_bundle_overrides_verification(self) -> None:
        """A custom CA bundle path takes precedence over verify_tls."""
        from unittest.mock import MagicMock, patch
        mock = MagicMock(status_code=200, content=b"ok")
        with patch("trading_bot.data.eod_fetcher.requests.get", return_value=mock) as mock_get:
            client = MassiveFlatFilesClient(
                endpoint="https://example.com",
                access_key="AKID-test", secret_key="test-secret",
                bucket="b",
                tls_ca_bundle="/etc/ssl/certs/massive-ca.pem",
            )
            client.get_object_text("k")
        assert mock_get.call_args.kwargs["verify"] == "/etc/ssl/certs/massive-ca.pem"

    def test_auth_mode_bearer_uses_bearer_header(self) -> None:
        from unittest.mock import MagicMock, patch
        mock = MagicMock(status_code=200, content=b"ok")
        with patch("trading_bot.data.eod_fetcher.requests.get", return_value=mock) as mock_get:
            client = MassiveFlatFilesClient(
                endpoint="https://example.com",
                access_key="my-access-key", secret_key="my-secret",
                bucket="b", auth_mode="bearer",
            )
            client.get_object_text("k")
        h = mock_get.call_args.kwargs["headers"]
        assert h["Authorization"] == "Bearer my-access-key"
        # No SigV4-specific headers when using bearer.
        assert "x-amz-date" not in h
        assert "x-amz-content-sha256" not in h

    def test_rejects_unknown_auth_mode(self) -> None:
        import pytest
        with pytest.raises(ValueError, match="auth_mode"):
            MassiveFlatFilesClient(
                endpoint="https://example.com",
                access_key="a", secret_key="b", bucket="c",
                auth_mode="oauth",
            )

    def test_get_raises_on_non_2xx(self) -> None:
        client = MassiveFlatFilesClient(
            endpoint="https://example.s3.endpoint",
            access_key="AKID-test",
            secret_key="test-secret",
            bucket="test-bucket",
            region="us-east-1",
        )
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        with patch("trading_bot.data.eod_fetcher.requests.get", return_value=mock_response):
            with pytest.raises(RuntimeError, match="404"):
                client.get_object_text("stocks/day-aggregates/2026-07-07.csv")

    def test_get_retries_on_5xx(self) -> None:
        client = MassiveFlatFilesClient(
            endpoint="https://example.s3.endpoint",
            access_key="AKID-test",
            secret_key="test-secret",
            bucket="test-bucket",
            region="us-east-1",
            max_retries=2,
            throttle_seconds=0,  # No real sleep in tests
        )
        fail = MagicMock(status_code=503, content=b"Service Unavailable")
        ok = MagicMock(status_code=200, content=_fake_csv().encode("utf-8"))
        with patch(
            "trading_bot.data.eod_fetcher.requests.get",
            side_effect=[fail, ok],
        ) as mock_get:
            body = client.get_object_text("stocks/day-aggregates/2026-07-07.csv")
        assert "AAPL" in body
        assert mock_get.call_count == 2


# ---------------------------------------------------------------------------
# End-to-end fetch_for_day — pulls, parses, filters
# ---------------------------------------------------------------------------


class TestFetchForDay:
    def test_filters_to_universe(self, tmp_path: Path) -> None:
        client = MassiveFlatFilesClient(
            endpoint="https://example.s3.endpoint",
            access_key="AKID-test",
            secret_key="test-secret",
            bucket="test-bucket",
            region="us-east-1",
            throttle_seconds=0,
        )
        mock_response = MagicMock(
            status_code=200, content=_fake_csv().encode("utf-8")
        )
        universe = {"AAPL", "NVDA"}
        with patch("trading_bot.data.eod_fetcher.requests.get", return_value=mock_response):
            df = client.fetch_for_day(
                "day-aggregates", date(2026, 7, 7), universe
            )
        assert sorted(df["ticker"].unique()) == ["AAPL", "NVDA"]
        assert len(df) == 2  # AAPL + NVDA, FOO filtered out

    def test_empty_universe_returns_empty_dataframe(self) -> None:
        client = MassiveFlatFilesClient(
            endpoint="https://example.s3.endpoint",
            access_key="AKID-test",
            secret_key="test-secret",
            bucket="test-bucket",
            region="us-east-1",
        )
        mock_response = MagicMock(
            status_code=200, content=_fake_csv().encode("utf-8")
        )
        with patch("trading_bot.data.eod_fetcher.requests.get", return_value=mock_response):
            df = client.fetch_for_day("day-aggregates", date(2026, 7, 7), set())
        assert df.empty
"""EOD fetcher for massive.com S3 flat-files.

Downloads daily CSV aggregates for our universe and writes them into the
long-term data store (``data_store.py``). Operates at end-of-day after
massive.com publishes the previous session's bars (~11:00 AM ET).

The signing is AWS Signature V4 implemented locally — no boto3 dependency.
If the project ever wants a heavier client, the ``MassiveFlatFilesClient``
class is the single point to swap.

Configuration via :class:`DataStoreSettings` from ``data_store.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import date, datetime, timezone
from typing import Iterable

import pandas as pd
import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AWS Signature V4
#
# Spec: https://docs.aws.amazon.com/general/latest/gr/sigv4_signing.html
# ---------------------------------------------------------------------------


def _sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign_aws_sig_v4(
    method: str,
    canonical_uri: str,
    canonical_querystring: str,
    canonical_headers: str,
    signed_headers: str,
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    amz_date: str,
) -> str:
    """Compute the AWS Signature V4 signature for a request.

    Returns the hex-encoded signature (the value placed in the
    ``Signature=`` query parameter or as the trailing component of the
    ``Authorization`` header). Uses the same shape as the official AWS
    reference test (canonical inputs and outputs).
    """
    canonical_request = (
        f"{method}\n{canonical_uri}\n{canonical_querystring}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"{algorithm}\n{amz_date}\n{credential_scope}\n"
        f"{_sha256_hex(canonical_request)}"
    )

    # Derive the signing key.
    k_date = _hmac_sha256(f"AWS4{secret_key}".encode("utf-8"), amz_date[:8])
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")

    return hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ---------------------------------------------------------------------------
# S3 key construction
# ---------------------------------------------------------------------------


def build_s3_key(
    product: str,
    as_of_date: date,
    key_template: str | None = None,
) -> str:
    """Return the S3 object key for a single day's flat-file.

    Default templates mirror the public massive.com docs example:
        stocks/day-aggregates/YYYY-MM-DD.csv
        stocks/minute-aggregates/YYYY-MM-DD.csv.gz

    For the actual hosted bucket (as of 2026-07), the prefix is
    ``us_stocks_sip/day_aggs_v1/YYYY/YYYY-MM-DD.csv.gz`` and
    ``us_stocks_sip/minute_aggs_v1/YYYY/YYYY-MM-DD.csv.gz``. Pass
    ``key_template`` to override per-product; the template supports
    these placeholders:

        ``{product}`` — e.g. "day-aggregates" or "minute-aggregates"
        ``{date}`` — ISO YYYY-MM-DD
        ``{year}`` — YYYY
        ``{month}`` — MM
        ``{day}`` — DD
    """
    if key_template is not None:
        return key_template.format(
            product=product,
            date=as_of_date.isoformat(),
            year=f"{as_of_date.year:04d}",
            month=f"{as_of_date.month:02d}",
            day=f"{as_of_date.day:02d}",
        )
    if product == "day-aggregates":
        suffix = ".csv"
    elif product == "minute-aggregates":
        suffix = ".csv.gz"
    else:
        suffix = ".csv"
    return f"stocks/{product}/{as_of_date.isoformat()}{suffix}"


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------


def parse_massive_day_aggregates_csv(csv_text: str) -> pd.DataFrame:
    """Parse a Massive.com day-aggregates CSV body into a DataFrame."""
    from io import StringIO

    return pd.read_csv(StringIO(csv_text))


# ---------------------------------------------------------------------------
# Universe filtering
# ---------------------------------------------------------------------------


def filter_csv_to_universe(df: pd.DataFrame, universe: Iterable[str]) -> pd.DataFrame:
    """Return only the rows whose ticker is in ``universe`` (case-insensitive)."""
    if not universe:
        return df.iloc[0:0].copy()
    upper_universe = {s.upper().strip() for s in universe}
    upper_tickers = df["ticker"].astype(str).str.upper().str.strip()
    return df.loc[upper_tickers.isin(upper_universe)].reset_index(drop=True)


# ---------------------------------------------------------------------------
# S3 client
# ---------------------------------------------------------------------------


class MassiveFlatFilesClient:
    """Thin client for downloading one flat-file at a time.

    Designed for daily batch use (one or two GETs per day), not streaming.
    Retries on 5xx with exponential backoff up to ``max_retries`` times.

    Addressing style: pass ``addressing_style="path"`` for S3-compatible
    endpoints that don't support virtual-hosted DNS (e.g. some CDNs, on-prem
    MinIO). Default is ``"virtual"`` (the AWS-native shape).

    Auth mode: ``"sigv4"`` (default) signs requests with AWS Signature V4 —
    works against real AWS S3 and most S3-compatible gateways. ``"bearer"``
    sends ``Authorization: Bearer <access_key>`` instead — useful for
    gateways (like massive.com's flat-files endpoint as of 2026-07) that
    expose S3-like paths but authenticate via a REST-API key.

    TLS verification defaults to **on**. To fetch from an endpoint that
    serves a self-signed certificate (e.g. massive.com's flat-files
    endpoint as of 2026-07), pass either:
      * ``verify_tls=False`` to disable verification (logs a loud warning at
        construction time so the audit trail is clear), OR
      * ``tls_ca_bundle="/path/to/ca.pem"`` to pin a specific CA bundle.

    Never call with both unset on an untrusted network.
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
        service: str = "s3",
        max_retries: int = 3,
        throttle_seconds: float = 0.2,
        timeout_seconds: int = 60,
        addressing_style: str = "virtual",
        verify_tls: bool = True,
        tls_ca_bundle: str | None = None,
        auth_mode: str = "sigv4",
    ) -> None:
        if addressing_style not in ("virtual", "path"):
            raise ValueError(
                f"addressing_style must be 'virtual' or 'path', got {addressing_style!r}"
            )
        if auth_mode not in ("sigv4", "bearer"):
            raise ValueError(
                f"auth_mode must be 'sigv4' or 'bearer', got {auth_mode!r}"
            )
        self.endpoint = endpoint.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.region = region
        self.service = service
        self.max_retries = max_retries
        self.throttle_seconds = throttle_seconds
        self.timeout_seconds = timeout_seconds
        self.addressing_style = addressing_style
        self.auth_mode = auth_mode

        # TLS verification: True by default; a CA bundle path overrides;
        # an explicit False disables with a loud audit-trail warning.
        self._verify: bool | str = tls_ca_bundle if tls_ca_bundle else verify_tls
        if self._verify is False:
            logger.warning(
                "TLS certificate verification DISABLED for S3 endpoint %s — "
                "data in transit is not authenticated. Only do this for "
                "trusted self-signed endpoints.",
                endpoint,
            )
            # Suppress the per-request InsecureRequestWarning since the
            # operator has already acknowledged the risk via config.
            try:
                import urllib3

                urllib3.disable_warnings(
                    urllib3.exceptions.InsecureRequestWarning
                )
            except ImportError:  # pragma: no cover (urllib3 is a requests dep)
                pass

    def _signing_key_headers(
        self, method: str, canonical_uri: str, canonical_querystring: str, payload: bytes
    ) -> dict[str, str]:
        """Return the headers required for a signed GET."""
        if self.auth_mode == "bearer":
            return {"Authorization": f"Bearer {self.access_key}"}

        amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        host = self._host()
        payload_hash = _sha256_hex(payload)
        # Include x-amz-checksum-mode in the signed headers so the server
        # accepts the request. This is a newer S3 feature (post-2024); the
        # `massive.com` S3-compatible gateway appears to require it on GETs.
        # When the header is present in the signed canonical request, S3
        # returns a `x-amz-checksum-*` response header that the client can
        # verify (we don't currently verify it — the data is gzipped and
        # the cost of a false positive is low).
        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-checksum-mode:ENABLED\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-checksum-mode;x-amz-content-sha256;x-amz-date"
        signature = sign_aws_sig_v4(
            method=method,
            canonical_uri=canonical_uri,
            canonical_querystring=canonical_querystring,
            canonical_headers=canonical_headers,
            signed_headers=signed_headers,
            payload_hash=payload_hash,
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
            service=self.service,
            amz_date=amz_date,
        )
        credential_scope = (
            f"{amz_date[:8]}/{self.region}/{self.service}/aws4_request"
        )
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Authorization": authorization,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "x-amz-checksum-mode": "ENABLED",
            "host": host,
        }

    def _host(self) -> str:
        """Return the host header value to sign against."""
        from urllib.parse import urlparse

        parsed = urlparse(self.endpoint)
        host = parsed.netloc or parsed.hostname or ""
        if self.addressing_style == "virtual":
            if host.startswith(f"{self.bucket}."):
                return host
            return f"{self.bucket}.{host}" if host else self.bucket
        # Path-style: bucket is in the URI, host is the bare endpoint.
        return host

    def _url_for(self, key: str) -> tuple[str, str]:
        """Return (url, canonical_uri) for an S3 GET on ``key``."""
        from urllib.parse import urlparse

        parsed = urlparse(self.endpoint)
        scheme = parsed.scheme or "https"
        host = self._host()
        base = f"{scheme}://{host}" if host else self.endpoint
        if self.addressing_style == "virtual":
            canonical_uri = "/" + key.lstrip("/")
            return base + canonical_uri, canonical_uri
        # Path-style: bucket is the first path segment of the canonical URI.
        canonical_uri = "/" + self.bucket + "/" + key.lstrip("/")
        return base + canonical_uri, canonical_uri

    def get_object_text(self, key: str) -> str:
        """GET an S3 object as text. Retries on 5xx with backoff.

        Auto-decompresses gzipped bodies (massive.com's minute-aggregates
        flat-files are stored as ``.csv.gz``). The ``Content-Encoding: gzip``
        header is sometimes absent on S3-compatible endpoints, so we sniff
        the magic bytes too.
        """
        url, canonical_uri = self._url_for(key)
        payload = b""
        attempt = 0
        last_exc: Exception | None = None
        while attempt < self.max_retries:
            # Only sleep AFTER a failed attempt. The first try should be
            # fast on the happy path; backoff on retry.
            if attempt > 0 and self.throttle_seconds > 0:
                time.sleep(self.throttle_seconds * (2 ** (attempt - 1)))
            headers = self._signing_key_headers("GET", canonical_uri, "", payload)
            try:
                response = requests.get(
                    url, headers=headers, timeout=self.timeout_seconds,
                    verify=self._verify,
                )
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("S3 GET failed (attempt %s): %s", attempt + 1, exc)
                attempt += 1
                continue

            if 500 <= response.status_code < 600:
                logger.warning(
                    "S3 GET %s returned %s (attempt %s)",
                    key, response.status_code, attempt + 1,
                )
                attempt += 1
                continue

            if response.status_code >= 400:
                raise RuntimeError(
                    f"S3 GET {key} failed with {response.status_code}: "
                    f"{response.text[:200]}"
                )

            body = response.content
            # Sniff gzip magic bytes (1f 8b) regardless of Content-Encoding.
            if body[:2] == b"\x1f\x8b":
                import gzip

                body = gzip.decompress(body)
            return body.decode("utf-8")

        raise RuntimeError(
            f"S3 GET {key} exhausted {self.max_retries} retries"
        ) from last_exc

    # Convenience: fetch + parse + filter in one call.
    def fetch_for_day(
        self, product: str, as_of_date: date, universe: Iterable[str]
    ) -> pd.DataFrame:
        """Fetch one day's flat-file and return rows for ``universe`` only."""
        key = build_s3_key(product, as_of_date)
        body = self.get_object_text(key)
        df = parse_massive_day_aggregates_csv(body)
        return filter_csv_to_universe(df, universe)
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    name: str
    asset_classes: frozenset[str]
    intervals: frozenset[str]
    required_environment: tuple[str, ...] = ()
    supports_screening: bool = False
    intraday_priority: int = 99

    def supports_interval(self, interval: str) -> bool:
        return str(interval).strip().lower() in self.intervals


@dataclass(frozen=True, slots=True)
class ProviderReadiness:
    name: str
    ready: bool
    reason: str


_PROVIDERS = {
    "alpaca": ProviderCapabilities(
        name="alpaca",
        asset_classes=frozenset({"equity"}),
        intervals=frozenset(
            {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "2h", "4h", "1d", "1mo", "3mo"}
        ),
        required_environment=("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"),
        intraday_priority=1,
    ),
    "finnhub": ProviderCapabilities(
        name="finnhub",
        asset_classes=frozenset({"equity"}),
        intervals=frozenset({"1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"}),
        required_environment=("FINNHUB_API_KEY",),
        intraday_priority=2,
    ),
    "polygon": ProviderCapabilities(
        name="polygon",
        asset_classes=frozenset({"equity"}),
        intervals=frozenset({"1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"}),
        required_environment=("POLYGON_API_KEY",),
        intraday_priority=0,
    ),
    "yfinance": ProviderCapabilities(
        name="yfinance",
        asset_classes=frozenset({"equity"}),
        intervals=frozenset(
            {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"}
        ),
        supports_screening=True,
        intraday_priority=3,
    ),
}


def get_provider_capabilities(name: str) -> ProviderCapabilities:
    normalized = str(name).strip().lower()
    try:
        return _PROVIDERS[normalized]
    except KeyError as exc:
        raise ValueError(f"Unknown market data provider '{name}'") from exc


def provider_readiness(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> ProviderReadiness:
    capabilities = get_provider_capabilities(name)
    values = os.environ if environ is None else environ
    missing = [key for key in capabilities.required_environment if not values.get(key)]
    if missing:
        return ProviderReadiness(
            name=capabilities.name,
            ready=False,
            reason="missing " + "/".join(missing),
        )
    return ProviderReadiness(name=capabilities.name, ready=True, reason="ok")


def order_provider_names(names: Sequence[str], interval: str | None) -> list[str]:
    """Return ``names`` ordered for the requested ``interval``.

    Daily intervals (anything that does not end in ``m`` or ``h``) keep the
    configured order. Intraday intervals are reordered by
    :attr:`ProviderCapabilities.intraday_priority`, and any provider whose
    declared capabilities do not include the interval is dropped so the
    fallback chain cannot attempt an unsupported fetch.
    """
    ordered = [str(name).strip().lower() for name in names]
    token = str(interval or "").strip().lower()
    if not (token.endswith("m") or token.endswith("h")):
        return ordered
    eligible = [
        name for name in ordered if get_provider_capabilities(name).supports_interval(token)
    ]
    eligible.sort(key=lambda name: get_provider_capabilities(name).intraday_priority)
    return eligible

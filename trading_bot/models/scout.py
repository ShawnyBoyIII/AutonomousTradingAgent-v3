from __future__ import annotations

from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import AliasChoices


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if isfinite(numeric) else None


class ScoutScreenerQuote(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    ticker: str = Field(validation_alias=AliasChoices("symbol"))
    source: str = ""
    quote_type: str = Field(default="EQUITY", validation_alias=AliasChoices("quoteType"))
    exchange: str = ""
    market_cap: float | None = Field(
        default=None, validation_alias=AliasChoices("marketCap", "intradaymarketcap")
    )
    price: float | None = Field(
        default=None, validation_alias=AliasChoices("regularMarketPrice", "intradayprice", "price")
    )
    avg_volume_3m: float | None = Field(
        default=None,
        validation_alias=AliasChoices("averageDailyVolume3Month", "avgdailyvol3m", "regularMarketVolume"),
    )
    day_volume: float | None = Field(
        default=None, validation_alias=AliasChoices("dayVolume", "dayvolume", "regularMarketVolume")
    )

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, value: object) -> str:
        return str(value).upper().strip() if value is not None else ""

    @field_validator("market_cap", "price", "avg_volume_3m", "day_volume", mode="before")
    @classmethod
    def _drop_non_finite(cls, value: object) -> float | None:
        return _coerce_float(value)


class ScoutCandidate(BaseModel):
    ticker: str
    scout_score: float = 0.0
    rank: int | None = None
    included: bool = False
    source_hits: int = 0
    source_names: list[str] = []
    market_cap: int | None = None
    price: float | None = None
    avg_dollar_volume: float = 0.0
    volume_ratio: float | None = None
    reasons: list[str] = []


class ScoutSummary(BaseModel):
    candidates: int = 0
    included: int = 0
    excluded: int = 0
    errors: int = 0


class ScoutResult(BaseModel):
    candidates: list[ScoutCandidate] = []
    included_symbols: list[str] = []
    summary: ScoutSummary = ScoutSummary()


class UniverseCandidatesSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: str = "universe"
    summary: ScoutSummary = ScoutSummary()
    candidates: list[ScoutCandidate] = []
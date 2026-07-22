"""Immutable approved-candidate contract.

Scan produces an :class:`ApprovedCandidate` for every symbol that
clears its quality/confidence gates. The candidate is written to a
JSON file that the paper-trade command consumes. The candidate carries
all the signal, sizing, and stop/target evidence so paper-trade does
not have to recompute it.

The contract is intentionally one-way: scan writes candidates; paper-
trade consumes them. There is no path from paper-trade back into a
candidate. If conditions change between scan and execution (kill
switch, circuit breaker, fresh cash/drawdown, stale data), paper-trade
rejects the candidate rather than mutating it.

Concrete fields (deliberately a subset of the row scan produces):

- ticker: str
- quality: str ("GREEN" | "YELLOW")
- timestamp: ISO-8601 UTC string of the bar the scan evaluated
- entry, stop, target: float (entry already accounts for slippage)
- qty: int (sized at scan time)
- rr, confidence, risk, allocation: float (snapshot for audit)
- strategy: str (e.g. "v3-trend_following", "v3-mean_reversion_oversold")
- v3_score, v3_confidence, v3_regime, v3_setup: Optional[float|str]
- supermodel_decision: str ("support" | "caution" | "block" | "no_signal")
- source_votes: list[dict] (serialized V2.5/V3 consensus votes)
- scan_id: str (unique identifier per scan run)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ApprovedCandidate:
    """Immutable scan-time decision consumed by paper-trade.

    The dataclass is ``frozen=True`` so the burner cannot accidentally
    rewrite a scan decision. Use :meth:`to_dict` for serialization and
    :meth:`from_dict` for parsing.
    """

    ticker: str
    quality: str
    timestamp: str
    entry: float
    stop: float
    target: float
    qty: int
    rr: float
    confidence: float
    risk: float
    allocation: float
    strategy: str
    supermodel_decision: str
    scan_id: str
    v3_score: float | None = None
    v3_confidence: str | None = None
    v3_regime: str | None = None
    v3_setup: str | None = None
    source_votes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ApprovedCandidate":
        return cls(
            ticker=str(payload["ticker"]),
            quality=str(payload["quality"]),
            timestamp=str(payload["timestamp"]),
            entry=float(payload["entry"]),
            stop=float(payload["stop"]),
            target=float(payload["target"]),
            qty=int(payload["qty"]),
            rr=float(payload["rr"]),
            confidence=float(payload["confidence"]),
            risk=float(payload["risk"]),
            allocation=float(payload["allocation"]),
            strategy=str(payload["strategy"]),
            supermodel_decision=str(payload["supermodel_decision"]),
            scan_id=str(payload["scan_id"]),
            v3_score=_optional_float(payload.get("v3_score")),
            v3_confidence=_optional_str(payload.get("v3_confidence")),
            v3_regime=_optional_str(payload.get("v3_regime")),
            v3_setup=_optional_str(payload.get("v3_setup")),
            source_votes=list(payload.get("source_votes") or []),
        )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def write_candidates_jsonl(
    candidates: list[ApprovedCandidate], path: Path
) -> Path:
    """Write a JSONL file of approved candidates.

    One JSON object per line. The burner reads the file line-by-line
    rather than scraping CLI output, eliminating the brittle
    ``grep | awk`` parse path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for candidate in candidates:
            handle.write(json.dumps(candidate.to_dict(), default=str) + "\n")
    return path


def read_candidate(ticker: str, path: Path) -> ApprovedCandidate | None:
    """Read the first candidate matching ``ticker``.

    Returns ``None`` if no candidate for ``ticker`` exists. This is the
    exact contract paper-trade uses to look up scan-approved entries.
    """
    if not path.exists():
        return None
    upper = ticker.upper()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(payload.get("ticker", "")).upper() != upper:
                continue
            return ApprovedCandidate.from_dict(payload)
    return None


def list_candidates(path: Path) -> list[ApprovedCandidate]:
    """Return every candidate in the JSONL file.

    The order matches the order scan wrote them.
    """
    if not path.exists():
        return []
    out: list[ApprovedCandidate] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.append(ApprovedCandidate.from_dict(payload))
    return out


def new_scan_id() -> str:
    """Return a unique scan identifier (UTC ISO without colons)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")

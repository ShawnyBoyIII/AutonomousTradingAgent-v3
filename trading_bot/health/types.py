from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Status = Literal["PASS", "WARN", "FAIL"]

_SEVERITY_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    observed: dict | None = None


@dataclass
class HealthReport:
    checks: list[CheckResult] = field(default_factory=list)
    generated_at: str = ""

    def worst_status(self) -> Status:
        if not self.checks:
            return "PASS"
        return max(self.checks, key=lambda c: _SEVERITY_RANK[c.status]).status

    def to_dict(self) -> dict:
        return {
            "worst_status": self.worst_status(),
            "generated_at": self.generated_at,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "detail": c.detail,
                    "observed": c.observed,
                }
                for c in self.checks
            ],
        }

from __future__ import annotations

from typing import Any


def format_advisory_report(report: dict[str, Any]) -> str:
    lines = ["ADVISORY LEARNER REPORT"]
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    lines.append(
        " ".join(
            [
                f"observations={int(summary.get('observations', 0))}",
                f"main={int(summary.get('main_recommendations', 0))}",
                f"cheap={int(summary.get('cheap_recommendations', 0))}",
                f"promote={int(summary.get('promoted_symbols', 0))}",
                f"avoid={int(summary.get('avoided_symbols', 0))}",
            ]
        )
    )
    for heading, key in (("MAIN MIDCAP", "main_midcap"), ("CHEAP STOCKS", "cheap_stocks")):
        rows = report.get(key, []) if isinstance(report.get(key), list) else []
        if not rows:
            continue
        lines.append(heading)
        for row in rows:
            lines.append(
                f"{row.get('ticker')} score={float(row.get('score', 0.0)):.2f} "
                f"approval_rate={float(row.get('approval_rate', 0.0)):.2f} "
                f"net_pnl={float(row.get('net_pnl', 0.0)):.2f}"
            )
    return "\n".join(lines)


def format_daily_report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    lines = [
        "# Daily Report",
        "",
        "## Summary",
        "",
        f"- Observations: {int(summary.get('observations', 0))}",
        f"- Main recommendations: {int(summary.get('main_recommendations', 0))}",
        f"- Cheap recommendations: {int(summary.get('cheap_recommendations', 0))}",
        f"- Promoted symbols: {int(summary.get('promoted_symbols', 0))}",
        f"- Avoided symbols: {int(summary.get('avoided_symbols', 0))}",
    ]
    for heading, key in (("Main Midcap Recommendations", "main_midcap"), ("Cheap Stock Ideas", "cheap_stocks")):
        rows = report.get(key, []) if isinstance(report.get(key), list) else []
        lines.extend(["", f"## {heading}", ""])
        if not rows:
            lines.append("- None")
            continue
        for row in rows:
            lines.append(
                f"- `{row.get('ticker')}` score={float(row.get('score', 0.0)):.2f} "
                f"approval_rate={float(row.get('approval_rate', 0.0)):.2f} net_pnl={float(row.get('net_pnl', 0.0)):.2f}"
            )
    return "\n".join(lines) + "\n"

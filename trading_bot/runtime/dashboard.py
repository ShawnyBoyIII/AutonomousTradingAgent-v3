from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from trading_bot.config.settings import Settings


def build_dashboard(settings: Settings, output_path: Path) -> Path:
    scan = _read_json(settings.app.scan_results_path)
    portfolio = _read_json(settings.app.portfolio_summary_path)
    report = _read_json(settings.app.dashboard_summary_path)
    backtest = _read_json(settings.app.backtest_summary_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        _render_dashboard(scan=scan, portfolio=portfolio, report=report, backtest=backtest),
        encoding="utf-8",
    )
    return output_path


def _read_json(path: str) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.exists():
        return {}
    return json.loads(candidate.read_text(encoding="utf-8"))


def _render_dashboard(
    scan: dict[str, Any],
    portfolio: dict[str, Any],
    report: dict[str, Any],
    backtest: dict[str, Any],
) -> str:
    portfolio_summary = portfolio.get("summary", {})
    report_summary = report.get("summary", {})
    backtest_summary = backtest.get("summary", {})
    candidates = scan.get("candidates", [])
    positions = portfolio.get("positions", [])
    decisions = report.get("recent_decisions", [])

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Autonomous Trading Agent</title>
  <style>
    body {{ margin: 24px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: #101418; color: #e6edf3; }}
    h1, h2 {{ margin: 0 0 12px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 16px 0; }}
    .card {{ background: #18202a; border: 1px solid #2f3b48; border-radius: 10px; padding: 14px; }}
    .label {{ color: #8b949e; font-size: 12px; }}
    .value {{ font-size: 24px; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 8px 0 20px; }}
    th, td {{ border-bottom: 1px solid #2f3b48; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ color: #8b949e; }}
    .GREEN {{ color: #3fb950; }}
    .YELLOW {{ color: #d29922; }}
    .REJECTED, .NO_SIGNAL {{ color: #f85149; }}
  </style>
</head>
<body>
  <h1>Autonomous Trading Agent</h1>
  <p class="label">Static local dashboard from JSON snapshots.</p>
  <section class="grid">
    {_card("Equity", _money(portfolio_summary.get("equity")))}
    {_card("Cash", _money(portfolio_summary.get("cash")))}
    {_card("Exposure", _pct(portfolio_summary.get("exposure")))}
    {_card("Net P/L", _money(report_summary.get("net_pnl")))}
    {_card("Backtest Trades", backtest_summary.get("trades", "n/a"))}
    {_card("Backtest Net P/L", _money(backtest_summary.get("net_pnl")))}
  </section>
  <h2>Scan Candidates</h2>
  {_table(candidates, ["ticker", "status", "quality", "reason", "confidence", "entry", "stop", "target"])}
  <h2>Positions</h2>
  {_table(positions, ["ticker", "quantity", "average_cost", "last_price", "market_value", "unrealized_pnl", "allocation"])}
  <h2>Recent Decisions</h2>
  {_table(decisions, ["timestamp", "command", "ticker", "status", "reason"])}
</body>
</html>
"""


def _card(label: str, value: object) -> str:
    return f'<div class="card"><div class="label">{html.escape(label)}</div><div class="value">{html.escape(str(value))}</div></div>'


def _table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return '<p class="label">No rows.</p>'
    head = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="{html.escape(str(row.get(column, "")))}">{html.escape(str(row.get(column, "")))}</td>'
            for column in columns
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _money(value: object) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(value: object) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "n/a"

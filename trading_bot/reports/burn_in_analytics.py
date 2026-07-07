from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from trading_bot.config.settings import Settings
from trading_bot.portfolio.ledger import PortfolioLedger


def _parse_decision_log(log_path: Path) -> list[dict[str, Any]]:
    """Parse decision-log.jsonl into a list of event dicts."""
    events: list[dict[str, Any]] = []
    if not log_path.exists():
        return events
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _parse_date(event: dict) -> datetime | None:
    """Extract datetime from an event dict."""
    for key in ("timestamp", "filled_at", "entry_at"):
        val = event.get(key)
        if val:
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
    # Try ISO date prefix from any string field
    for val in event.values():
        if isinstance(val, str) and len(val) >= 10:
            try:
                return datetime.fromisoformat(val[:19])
            except (ValueError, TypeError):
                continue
    return None


def compute_trade_summary(events: list[dict], ledger: PortfolioLedger) -> dict[str, Any]:
    """Compute trade-level summary from filled events."""
    fills = [e for e in events if e.get("command") in ("paper-trade", "manage-positions") and e.get("status") == "FILLED"]

    buys = [e for e in fills if e.get("command") == "paper-trade"]
    sells = [e for e in fills if e.get("command") == "manage-positions"]

    ledger_rows = []
    try:
        ledger_rows = ledger.list_order_rows()
    except Exception:
        ledger_rows = []

    ledger_sell_rows = [row for row in ledger_rows if row.get("side") == "SELL"]
    if ledger_sell_rows:
        total_pnl = sum(float(row.get("pnl", 0.0)) for row in ledger_sell_rows)
        total_fees = sum(float(row.get("fees", 0.0)) for row in ledger_rows)
        wins = sum(1 for row in ledger_sell_rows if float(row.get("pnl", 0.0)) > 0)
        losses = sum(1 for row in ledger_sell_rows if float(row.get("pnl", 0.0)) < 0)
        flat = sum(1 for row in ledger_sell_rows if float(row.get("pnl", 0.0)) == 0)
        total_closed = len(ledger_sell_rows)
        gross_profit = sum(float(row.get("pnl", 0.0)) for row in ledger_sell_rows if float(row.get("pnl", 0.0)) > 0)
        gross_loss_abs = abs(sum(float(row.get("pnl", 0.0)) for row in ledger_sell_rows if float(row.get("pnl", 0.0)) < 0))
        profit_factor = round(gross_profit / gross_loss_abs, 2) if gross_loss_abs > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0.0)
        avg_win = round(gross_profit / wins, 2) if wins > 0 else 0.0
        avg_loss_total = sum(float(row.get("pnl", 0.0)) for row in ledger_sell_rows if float(row.get("pnl", 0.0)) < 0)
        avg_loss = round(avg_loss_total / losses, 2) if losses > 0 else 0.0
        pnl_by_ticker: dict[str, list[float | None]] = defaultdict(list)
        for row in ledger_sell_rows:
            pnl_by_ticker[str(row.get("ticker", ""))].append(float(row.get("pnl", 0.0)))
        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0.0
        return {
            "total_fills": len(fills),
            "buys": len(buys),
            "sells": len(sells),
            "closed_trades": total_closed,
            "wins": wins,
            "losses": losses,
            "flat": flat,
            "win_rate_pct": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "total_fees": round(total_fees, 2),
            "profit_factor": profit_factor,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "pnl_by_ticker": {
                ticker: [round(p, 2) if p is not None else None for p in pnls]
                for ticker, pnls in pnl_by_ticker.items()
            },
        }

    # Build P&L by matching buys with sells
    # First populate open positions from buys
    open_positions: dict[str, list[dict]] = defaultdict(list)
    for buy in buys:
        ticker = buy.get("ticker", "")
        open_positions[ticker].append(buy)

    pnl_by_ticker: dict[str, list[float]] = defaultdict(list)
    total_pnl = 0.0
    total_fees = 0.0
    wins = 0
    losses = 0
    flat = 0

    for sell in sells:
        ticker = sell.get("ticker", "")
        fill_price = float(sell.get("fill_price", 0))
        quantity = int(sell.get("quantity", 0))
        fees = float(sell.get("fees", 0))
        total_fees += fees

        # Find matching buy
        if open_positions[ticker]:
            buy = open_positions[ticker].pop(0)
            buy_price = float(buy.get("fill_price", 0))
            buy_qty = int(buy.get("quantity", 0))
            buy_fees = float(buy.get("fees", 0))
            total_fees += buy_fees

            pnl = (fill_price - buy_price) * min(quantity, buy_qty) - buy_fees - fees
            total_pnl += pnl
            pnl_by_ticker[ticker].append(pnl)

            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            else:
                flat += 1
        else:
            # Sell without matching buy (orphan)
            pnl = -fill_price * quantity - fees
            total_pnl += pnl
            pnl_by_ticker[ticker].append(pnl)
            losses += 1

    # Remaining open positions
    for ticker, positions in open_positions.items():
        for pos in positions:
            pnl_by_ticker[ticker].append(None)  # still open

    total_closed = wins + losses + flat
    win_rate = (wins / total_closed * 100) if total_closed > 0 else 0.0
    gross_profit = sum(p for pnls in pnl_by_ticker.values() for p in pnls if p is not None and p > 0)
    gross_loss_abs = abs(sum(p for pnls in pnl_by_ticker.values() for p in pnls if p is not None and p < 0))
    profit_factor = round(gross_profit / gross_loss_abs, 2) if gross_loss_abs > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0.0)
    avg_win = round(gross_profit / wins, 2) if wins > 0 else 0.0
    avg_loss_total = sum(p for pnls in pnl_by_ticker.values() for p in pnls if p is not None and p < 0)
    avg_loss = round(avg_loss_total / losses, 2) if losses > 0 else 0.0

    return {
        "total_fills": len(fills),
        "buys": len(buys),
        "sells": len(sells),
        "closed_trades": total_closed,
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "win_rate_pct": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "pnl_by_ticker": {
            ticker: [round(p, 2) if p is not None else None for p in pnls]
            for ticker, pnls in pnl_by_ticker.items()
        },
    }


def compute_signal_summary(events: list[dict]) -> dict[str, Any]:
    """Compute signal quality and scan results summary."""
    scan_events = [e for e in events if e.get("command") == "scan"]

    status_counts = Counter(e.get("status") for e in scan_events)

    # Quality breakdown
    quality_counts = Counter(e.get("quality") for e in scan_events if e.get("quality"))

    # Rejection reasons
    rejection_reasons = Counter()
    for e in scan_events:
        if e.get("status") == "REJECTED":
            reason = e.get("reason", "unknown")
            rejection_reasons[reason] += 1

    # Confidence distribution
    confidences = [e.get("confidence", 0) for e in scan_events if e.get("confidence")]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    # Approved signals that led to trades
    approved = [e for e in scan_events if e.get("status") == "APPROVED"]
    approved_with_details = [e for e in approved if e.get("entry")]

    return {
        "total_scans": len(scan_events),
        "status_counts": dict(status_counts),
        "quality_counts": dict(quality_counts),
        "rejection_reasons": dict(rejection_reasons.most_common(10)),
        "avg_confidence": round(avg_confidence, 3),
        "approved_count": status_counts.get("APPROVED", 0),
        "approved_with_entry": len(approved_with_details),
    }


def compute_exit_summary(events: list[dict]) -> dict[str, Any]:
    """Compute position exit analysis."""
    manage_events = [e for e in events if e.get("command") == "manage-positions"]
    exits = [e for e in manage_events if e.get("status") == "FILLED"]

    exit_reasons = Counter()
    for e in exits:
        reason = e.get("reason", "unknown")
        exit_reasons[reason] += 1

    # P&L by exit type
    pnl_by_exit: dict[str, list[float]] = defaultdict(list)
    for e in exits:
        reason = e.get("reason", "unknown")
        # Estimate P&L from fill data if available
        if e.get("fill_price") and e.get("quantity"):
            pnl_by_exit[reason].append(float(e.get("fill_price", 0)))

    return {
        "total_exits": len(exits),
        "exit_reasons": dict(exit_reasons.most_common(10)),
        "pnl_by_exit_type": {
            reason: sum(pnls) for reason, pnls in pnl_by_exit.items()
        },
    }


def compute_counter_thesis_summary(events: list[dict]) -> dict[str, Any]:
    """Compute counter-thesis effectiveness."""
    ct_events = [e for e in events if "counter_thesis" in e or e.get("counter_thesis_block")]
    ct_blocks = [e for e in events if e.get("counter_thesis_block") is True]
    ct_scaled = [e for e in events if "confidence_multiplier" in e and e.get("confidence_multiplier", 1.0) != 1.0]
    ct_exits = [e for e in events if e.get("reason") == "counter_thesis"]

    # Severity breakdown
    severities = Counter()
    for e in ct_events:
        ct = e.get("counter_thesis", {})
        if isinstance(ct, dict):
            severity = ct.get("severity", "unknown")
            severities[severity] += 1

    # Finding types
    findings = Counter()
    for e in ct_events:
        ct = e.get("counter_thesis", {})
        if isinstance(ct, dict):
            for f in ct.get("findings", []):
                if isinstance(f, dict):
                    findings[f.get("type", "unknown")] += 1
                elif isinstance(f, str):
                    findings[f] += 1

    return {
        "total_with_findings": len(ct_events),
        "total_blocked": len(ct_blocks),
        "total_scaled": len(ct_scaled),
        "total_ct_exits": len(ct_exits),
        "block_rate_pct": round(len(ct_blocks) / max(len(ct_events), 1) * 100, 1),
        "severity_counts": dict(severities),
        "top_findings": dict(findings.most_common(10)),
    }


def compute_risk_summary(events: list[dict]) -> dict[str, Any]:
    """Compute risk management metrics."""
    ks_events = [e for e in events if e.get("status") == "KILL_SWITCH"]
    cb_events = [e for e in events if e.get("status") == "CIRCUIT_BREAKER"]
    stale_events = [e for e in events if e.get("reason") == "stale market data"]
    validation_errors = [e for e in events if e.get("status") == "VALIDATION_ERROR"]

    cb_reasons = Counter()
    for e in cb_events:
        cb_reasons[e.get("reason", "unknown")] += 1

    stale_by_command = Counter()
    for e in stale_events:
        stale_by_command[e.get("command", "unknown")] += 1

    return {
        "kill_switch_triggers": len(ks_events),
        "circuit_breaker_triggers": len(cb_events),
        "circuit_breaker_reasons": dict(cb_reasons),
        "stale_data_rejections": len(stale_events),
        "stale_by_command": dict(stale_by_command),
        "validation_errors": len(validation_errors),
    }


def compute_ticker_performance(events: list[dict]) -> dict[str, Any]:
    """Compute per-ticker performance metrics."""
    ticker_events: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        ticker = e.get("ticker")
        if ticker:
            ticker_events[ticker].append(e)

    performance: dict[str, dict] = {}
    for ticker, t_events in ticker_events.items():
        scans = [e for e in t_events if e.get("command") == "scan"]
        buys = [e for e in t_events if e.get("command") == "paper-trade" and e.get("status") == "FILLED"]
        sells = [e for e in t_events if e.get("command") == "manage-positions" and e.get("status") == "FILLED"]

        approved = sum(1 for e in scans if e.get("status") == "APPROVED")
        rejected = sum(1 for e in scans if e.get("status") == "REJECTED")

        performance[ticker] = {
            "total_events": len(t_events),
            "scans": len(scans),
            "approved_signals": approved,
            "rejected_signals": rejected,
            "buys": len(buys),
            "sells": len(sells),
        }

    return dict(sorted(performance.items(), key=lambda x: x[1]["total_events"], reverse=True))


def compute_time_analysis(events: list[dict]) -> dict[str, Any]:
    """Compute time-based analysis (hourly distribution, daily trends)."""
    timed_events = []
    for e in events:
        dt = _parse_date(e)
        if dt:
            timed_events.append((dt, e))

    if not timed_events:
        return {"error": "no_timestamped_events"}

    timed_events.sort(key=lambda x: x[0])
    first_event = timed_events[0][0]
    last_event = timed_events[-1][0]
    duration_hours = max((last_event - first_event).total_seconds() / 3600, 0.001)

    # Hourly distribution
    hourly_counts = Counter()
    for dt, _ in timed_events:
        hourly_counts[dt.hour] += 1

    # Daily distribution
    daily_counts = Counter()
    for dt, _ in timed_events:
        daily_counts[dt.strftime("%Y-%m-%d")] += 1

    return {
        "first_event": first_event.isoformat(),
        "last_event": last_event.isoformat(),
        "duration_hours": round(duration_hours, 1),
        "events_per_hour": round(len(events) / duration_hours, 1),
        "hourly_distribution": {str(h): c for h, c in sorted(hourly_counts.items())},
        "daily_distribution": dict(sorted(daily_counts.items())),
    }


def generate_recommendations(
    trade_summary: dict,
    signal_summary: dict,
    risk_summary: dict,
    ct_summary: dict,
    exit_summary: dict,
) -> list[str]:
    """Generate actionable recommendations based on analytics."""
    recs: list[str] = []

    # Stale data
    stale = risk_summary.get("stale_data_rejections", 0)
    total = risk_summary.get("circuit_breaker_triggers", 0) + stale + trade_summary.get("total_fills", 0)
    if stale > 0 and stale / max(total, 1) > 0.5:
        recs.append(
            f"CRITICAL: {stale} stale data rejections ({stale/max(total,1)*100:.0f}% of events). "
            "Increase max_data_age_minutes in config or reduce scan-trade gap."
        )

    # Win rate
    win_rate = trade_summary.get("win_rate_pct", 0)
    closed = trade_summary.get("closed_trades", 0)
    if closed >= 5 and win_rate < 40:
        recs.append(
            f"Low win rate: {win_rate}% ({trade_summary.get('wins', 0)}W/{trade_summary.get('losses', 0)}L over {closed} trades). "
            "Consider tightening entry criteria or adjusting signal confidence threshold."
        )

    # Counter-thesis blocks
    if ct_summary.get("total_blocked", 0) > 0:
        block_rate = ct_summary.get("block_rate_pct", 0)
        if block_rate > 30:
            recs.append(
                f"High counter-thesis block rate: {block_rate}%. "
                "Review counter_thesis thresholds or market regime alignment."
            )

    # Exit distribution
    exit_reasons = exit_summary.get("exit_reasons", {})
    stops = exit_reasons.get("stop_loss", 0)
    targets = exit_reasons.get("profit_target", 0)
    if stops > targets and stops >= 3:
        recs.append(
            f"More stop losses ({stops}) than profit targets ({targets}). "
            "Consider adjusting entry timing or profit target levels."
        )

    # Rejection patterns
    rejection_reasons = signal_summary.get("rejection_reasons", {})
    if rejection_reasons.get("daily regime not bullish", 0) > 100:
        recs.append(
            "High 'daily regime not bullish' rejections. "
            "Market may be in a non-favorable regime - consider reducing position sizes."
        )

    # Low trade frequency
    if trade_summary.get("total_fills", 0) < 5:
        recs.append(
            "Low trade frequency. Consider expanding universe or relaxing confidence thresholds."
        )

    # P&L
    total_pnl = trade_summary.get("total_pnl", 0)
    if total_pnl < 0 and trade_summary.get("closed_trades", 0) >= 3:
        recs.append(
            f"Negative P&L: ${total_pnl:.2f}. Review trade selection and risk management parameters."
        )

    if not recs:
        recs.append("System operating within normal parameters. Continue monitoring.")

    return recs


def compute_burn_in_report(
    log_path: Path | str,
    db_path: Path | str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Compute comprehensive burn-in analytics report.

    Args:
        log_path: Path to decision-log.jsonl
        db_path: Path to SQLite state database
        settings: Optional settings for config-aware analysis

    Returns:
        Dict with all analytics sections
    """
    log_path = Path(log_path)
    db_path = Path(db_path)

    events = _parse_decision_log(log_path)

    ledger = PortfolioLedger(db_path) if db_path.exists() else None
    state = ledger.ensure_portfolio_state() if ledger else None

    # Trade summary works without ledger (uses event data directly)
    from unittest.mock import MagicMock
    mock_ledger = MagicMock()
    mock_ledger.list_order_rows.return_value = []
    trade_summary = compute_trade_summary(events, ledger or mock_ledger)
    signal_summary = compute_signal_summary(events)
    exit_summary = compute_exit_summary(events)
    ct_summary = compute_counter_thesis_summary(events)
    risk_summary = compute_risk_summary(events)
    ticker_perf = compute_ticker_performance(events)
    time_analysis = compute_time_analysis(events)

    recommendations = generate_recommendations(
        trade_summary, signal_summary, risk_summary, ct_summary, exit_summary
    )

    report = {
        "generated_at": datetime.now().isoformat(),
        "log_path": str(log_path),
        "db_path": str(db_path),
        "portfolio": {
            "cash": round(state.cash, 2) if state else None,
            "equity": round(state.equity, 2) if state else None,
            "realized_pnl": round(state.realized_pnl, 2) if state else None,
            "open_positions": len(state.positions) if state else 0,
        },
        "trades": trade_summary,
        "signals": signal_summary,
        "exits": exit_summary,
        "counter_thesis": ct_summary,
        "risk": risk_summary,
        "ticker_performance": ticker_perf,
        "time_analysis": time_analysis,
        "recommendations": recommendations,
    }

    return report


def format_report(report: dict[str, Any]) -> str:
    """Format analytics report as human-readable text."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("  BURN-IN ANALYTICS REPORT")
    lines.append("=" * 60)
    lines.append("")

    # Portfolio
    port = report.get("portfolio", {})
    lines.append("PORTFOLIO")
    lines.append("-" * 40)
    cash = port.get('cash') or 0
    equity = port.get('equity') or 0
    realized = port.get('realized_pnl') or 0
    lines.append(f"  Cash:           ${cash:,.2f}")
    lines.append(f"  Equity:         ${equity:,.2f}")
    lines.append(f"  Realized P&L:   ${realized:,.2f}")
    lines.append(f"  Open Positions: {port.get('open_positions', 0)}")
    lines.append("")

    # Trades
    trades = report.get("trades", {})
    lines.append("TRADES")
    lines.append("-" * 40)
    lines.append(f"  Total Fills:    {trades.get('total_fills', 0)}")
    lines.append(f"  Closed Trades:  {trades.get('closed_trades', 0)}")
    lines.append(f"  Wins:           {trades.get('wins', 0)}")
    lines.append(f"  Losses:         {trades.get('losses', 0)}")
    lines.append(f"  Flat:           {trades.get('flat', 0)}")
    lines.append(f"  Win Rate:       {trades.get('win_rate_pct', 0):.1f}%")
    lines.append(f"  Profit Factor:  {trades.get('profit_factor', 0):.2f}")
    lines.append(f"  Avg Win:        ${trades.get('avg_win', 0):,.2f}")
    lines.append(f"  Avg Loss:       ${trades.get('avg_loss', 0):,.2f}")
    lines.append(f"  Total P&L:      ${trades.get('total_pnl', 0):,.2f}")
    lines.append(f"  Total Fees:     ${trades.get('total_fees', 0):,.2f}")

    if trades.get("pnl_by_ticker"):
        lines.append("")
        lines.append("  P&L by Ticker:")
        for ticker, pnls in trades["pnl_by_ticker"].items():
            total = sum(p for p in pnls if p is not None)
            closed = sum(1 for p in pnls if p is not None)
            status = "OPEN" if len(pnls) != closed else "CLOSED"
            lines.append(f"    {ticker:10s} {status:6s} {closed:2d} trades  ${total:8.2f}")
    lines.append("")

    # Signals
    signals = report.get("signals", {})
    lines.append("SIGNALS")
    lines.append("-" * 40)
    lines.append(f"  Total Scans:       {signals.get('total_scans', 0)}")
    lines.append(f"  Approved:          {signals.get('approved_count', 0)}")
    lines.append(f"  Avg Confidence:    {signals.get('avg_confidence', 0):.3f}")

    status_counts = signals.get("status_counts", {})
    if status_counts:
        lines.append("")
        lines.append("  Status Breakdown:")
        for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
            lines.append(f"    {status:15s} {count:6d}")

    rejection_reasons = signals.get("rejection_reasons", {})
    if rejection_reasons:
        lines.append("")
        lines.append("  Top Rejection Reasons:")
        for reason, count in list(rejection_reasons.items())[:5]:
            lines.append(f"    {reason:40s} {count:5d}")
    lines.append("")

    # Risk
    risk = report.get("risk", {})
    lines.append("RISK MANAGEMENT")
    lines.append("-" * 40)
    lines.append(f"  Kill Switch Triggers:    {risk.get('kill_switch_triggers', 0)}")
    lines.append(f"  Circuit Breaker Triggers:{risk.get('circuit_breaker_triggers', 0)}")
    lines.append(f"  Stale Data Rejections:   {risk.get('stale_data_rejections', 0)}")
    lines.append(f"  Validation Errors:       {risk.get('validation_errors', 0)}")
    lines.append("")

    # Counter-thesis
    ct = report.get("counter_thesis", {})
    lines.append("COUNTER-THESIS (V3)")
    lines.append("-" * 40)
    lines.append(f"  Total with Findings: {ct.get('total_with_findings', 0)}")
    lines.append(f"  Blocked:             {ct.get('total_blocked', 0)}")
    lines.append(f"  Scaled:              {ct.get('total_scaled', 0)}")
    lines.append(f"  Block Rate:          {ct.get('block_rate_pct', 0):.1f}%")
    lines.append("")

    # Time analysis
    time_a = report.get("time_analysis", {})
    if "error" not in time_a:
        lines.append("TIME ANALYSIS")
        lines.append("-" * 40)
        lines.append(f"  Duration:        {time_a.get('duration_hours', 0):.1f} hours")
        lines.append(f"  Events/Hour:     {time_a.get('events_per_hour', 0):.1f}")
        lines.append("")

    # Recommendations
    recs = report.get("recommendations", [])
    lines.append("RECOMMENDATIONS")
    lines.append("-" * 40)
    for i, rec in enumerate(recs, 1):
        lines.append(f"  {i}. {rec}")
    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)

from __future__ import annotations

import html
import json
import logging
import socketserver
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from trading_bot.config.settings import Settings

logger = logging.getLogger(__name__)

# Simple LRU cache for position price enrichment (TTL-based)
_price_cache: OrderedDict[str, tuple[float, float]] = OrderedDict()
_price_cache_timestamps: dict[str, float] = {}
_CACHE_TTL_SECONDS = 30.0  # Cache fresh for 30 seconds
_CACHE_MAX_SIZE = 100  # Max entries to keep in memory


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


class DashboardServer:
    """Local HTTP server serving a live dashboard from state JSON files.

    Binds to localhost only (127.0.0.1) per the security hardening policy
    (AGENTS.md: "Dashboard binding: Binds to localhost only (127.0.0.1)").
    Serves:
      GET /           → live HTML dashboard (auto-refresh every 5s)
      GET /api/state  → JSON snapshot of all dashboard data
      GET /healthz    → "ok"
    """

    def __init__(
        self,
        settings: Settings,
        host: str = "127.0.0.1",
        port: int = 8000,
        decision_log_path: str | None = None,
        strategy_log_path: str | None = None,
    ) -> None:
        self.settings = settings
        self.host = host
        self.port = port
        self._httpd: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._state_root = self._resolve_state_root()
        self._decision_log_path = decision_log_path or str(
            Path(self.settings.app.log_dir) / "decision-log.jsonl"
        )
        self._strategy_log_path = strategy_log_path or str(
            Path(self.settings.app.log_dir) / "strategy_results.jsonl"
        )

    def _resolve_state_root(self) -> Path:
        state_db = Path(self.settings.app.state_db_path)
        if state_db.parent and str(state_db.parent) not in (".", ""):
            return state_db.parent
        return Path("state")

    def start(self) -> str:
        """Start the server in a daemon thread. Returns the bound URL."""
        if self._httpd is not None:
            return self.url
        handler = _make_handler(self)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._httpd = _Server((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="tradingbot-dashboard", daemon=True
        )
        self._thread.start()
        logger.info(f"Dashboard serving on {self.url} (localhost only)")
        return self.url

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        self._thread = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _resolve_optional_deps(self) -> dict[str, Any]:
        """Resolve optional dependencies used by :meth:`snapshot`.

        Returns a dict of lazily-imported symbols keyed by their
        canonical name so the snapshot method stays clean.
        """
        deps: dict[str, Any] = {}
        try:
            import pathlib
            from trading_bot.portfolio.ledger import PortfolioLedger
            from trading_bot.safety.kill_switch import is_trading_halted
            deps["PortfolioLedger"] = PortfolioLedger
            deps["is_trading_halted"] = is_trading_halted
            deps["_pathlib"] = pathlib
        except Exception:
            pass  # Ledger / kill-switch unavailable
        try:
            from trading_bot.monitoring.drawdown import compute_drawdown_from_ledger
            deps["compute_drawdown_from_ledger"] = compute_drawdown_from_ledger
        except Exception:
            pass
        try:
            from trading_bot.data.market_data import fetch_bars
            from trading_bot.strategy.market_regime import detect_market_regime
            deps["fetch_bars"] = fetch_bars
            deps["detect_market_regime"] = detect_market_regime
        except Exception:
            pass
        try:
            from trading_bot.strategy.strategy_tracker import strategy_summary
            deps["strategy_summary"] = strategy_summary
        except Exception:
            pass
        return deps

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the current dashboard state.

        Pure reads against JSON/JSONL files; safe to call from the request
        thread. Never raises — missing files yield empty sections.
        """
        scan = _read_json(self.settings.app.scan_results_path)
        portfolio = _read_json(self.settings.app.portfolio_summary_path)
        report = _read_json(self.settings.app.dashboard_summary_path)
        backtest = _read_json(self.settings.app.backtest_summary_path)
        decisions = _read_jsonl_tail(self._decision_log_path, limit=50)
        strategy_results = _read_jsonl_tail(self._strategy_log_path, limit=50)

        deps = self._resolve_optional_deps()

        ledger = None
        kill_active = False
        if "PortfolioLedger" in deps and "is_trading_halted" in deps:
            try:
                ledger = deps["PortfolioLedger"](deps["_pathlib"].Path(self.settings.app.state_db_path))
                kill_state = deps["is_trading_halted"](ledger)
                kill_active = kill_state.enabled
            except Exception:
                pass

        # Enrich open positions with live prices + computed metrics
        raw_positions = portfolio.get("positions", [])
        enriched_positions = _enrich_positions(raw_positions, self.settings)
        if enriched_positions:
            portfolio["positions"] = enriched_positions

        # Circuit breaker metrics
        consecutive_losses = 0
        if ledger:
            try:
                consecutive_losses = ledger.get_consecutive_losses()
            except Exception:
                pass

        drawdown_metrics = None
        if ledger and "compute_drawdown_from_ledger" in deps:
            try:
                drawdown_metrics = deps["compute_drawdown_from_ledger"](ledger)
            except Exception:
                pass

        # Market regime detection (SPY as primary benchmark)
        market_regime = None
        if "fetch_bars" in deps and "detect_market_regime" in deps:
            try:
                spy_daily = deps["fetch_bars"]("SPY", period="1y", interval="1d")
                if not spy_daily.empty:
                    regime, metrics = deps["detect_market_regime"](spy_daily)
                    market_regime = {
                        "regime": regime.value,
                        "adx": round(metrics.adx, 1),
                        "volatility_percentile": round(metrics.volatility_percentile * 100, 1),
                        "price_vs_ema20": round(metrics.price_vs_ema20, 2),
                        "price_vs_sma50": round(metrics.price_vs_sma50, 2),
                        "momentum": round(metrics.momentum, 2),
                    }
            except Exception:
                pass

        # Strategy performance attribution
        strategy_attribution = None
        if "strategy_summary" in deps:
            try:
                summary = deps["strategy_summary"](Path(self.settings.app.log_dir), window=50)
                if summary:
                    strategy_attribution = summary
            except Exception:
                pass

        return {
            "scan": scan,
            "portfolio": portfolio,
            "report": report,
            "backtest": backtest,
            "decisions": decisions,
            "strategy_results": strategy_results,
            "kill_switch": {
                "checked": ledger is not None,
                "active": bool(kill_active),
            },
            "circuit_breaker": {
                "consecutive_losses": consecutive_losses,
                "drawdown_current_pct": round(drawdown_metrics.current_drawdown_pct, 2) if drawdown_metrics else None,
                "drawdown_max_pct": round(drawdown_metrics.max_drawdown_pct, 2) if drawdown_metrics else None,
            },
            "market_regime": market_regime,
            "strategy_attribution": strategy_attribution,
            "generated_at": _now_iso(),
        }


def serve_dashboard(
    settings: Settings,
    host: str = "127.0.0.1",
    port: int = 8000,
    block: bool = True,
    decision_log_path: str | None = None,
    strategy_log_path: str | None = None,
) -> DashboardServer:
    """Construct and start a DashboardServer.

    When ``block=True`` (default for CLI use), this blocks the calling thread
    until Ctrl-C or SIGTERM. When ``block=False``, the server runs in a daemon
    thread and control returns immediately — useful for tests.
    """
    import signal

    server = DashboardServer(
        settings,
        host=host,
        port=port,
        decision_log_path=decision_log_path,
        strategy_log_path=strategy_log_path,
    )
    url = server.start()
    logger.info(f"Dashboard live at {url}")

    if block:
        shutdown_requested = threading.Event()

        def _signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, shutting down...")
            shutdown_requested.set()

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        try:
            # Wait for shutdown signal
            while not shutdown_requested.is_set():
                shutdown_requested.wait(timeout=1.0)
        finally:
            logger.info("Stopping dashboard server...")
            server.stop()
            logger.info("Dashboard server stopped")

    return server


# ---------------------------------------------------------------------------
# Handler factory + rendering (kept private to this module)
# ---------------------------------------------------------------------------


def _make_handler(server: DashboardServer) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            """Log HTTP requests for observability."""
            logger.info(f"dashboard: {args[0] if args else 'request'}")

        def do_GET(self) -> None:  # noqa: N802 - http.server contract
            try:
                if self.path in ("/", "/index.html"):
                    self._serve_html()
                elif self.path == "/api/state":
                    self._serve_json()
                elif self.path == "/healthz":
                    self._respond(HTTPStatus.OK, b"ok", "text/plain")
                else:
                    self._respond(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            except Exception as exc:  # never crash the server thread
                logger.exception("dashboard handler error", exc_info=exc)
                self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, b"error", "text/plain")

        def do_POST(self) -> None:  # noqa: N802 - http.server contract
            try:
                if self.path == "/api/kill-switch":
                    self._handle_kill_switch_toggle()
                else:
                    self._respond(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            except Exception as exc:
                logger.exception("dashboard POST error", exc_info=exc)
                self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, b"error", "text/plain")

        def _handle_kill_switch_toggle(self) -> None:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self._respond(HTTPStatus.BAD_REQUEST, b"invalid json", "text/plain")
                return
            action = payload.get("action", "")
            reason = payload.get("reason", "dashboard toggle")
            result = _toggle_kill_switch(server.settings, action, reason)
            if result.get("success"):
                self._respond(HTTPStatus.OK, json.dumps(result).encode("utf-8"), "application/json")
            else:
                self._respond(HTTPStatus.BAD_REQUEST, json.dumps(result).encode("utf-8"), "application/json")

        def _serve_html(self) -> None:
            snapshot = server.snapshot()
            body = _render_live_dashboard(snapshot).encode("utf-8")
            self._respond(HTTPStatus.OK, body, "text/html; charset=utf-8")

        def _serve_json(self) -> None:
            body = json.dumps(server.snapshot(), default=str).encode("utf-8")
            self._respond(HTTPStatus.OK, body, "application/json")

        def _respond(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def version_string(self) -> str:
            return "tradingbot-dashboard/1.0"

    return _Handler


# ---------------------------------------------------------------------------
# Live dashboard rendering — reuses the static renderer's primaries/cards but
# adds a live activity feed + kill-switch banner + auto-refresh.
# ---------------------------------------------------------------------------


def _render_live_dashboard(snapshot: dict[str, Any]) -> str:
    scan = snapshot.get("scan", {})
    portfolio = snapshot.get("portfolio", {})
    report = snapshot.get("report", {})
    backtest = snapshot.get("backtest", {})
    decisions = snapshot.get("decisions", [])
    strategy_results = snapshot.get("strategy_results", [])
    kill = snapshot.get("kill_switch", {})
    generated_at = snapshot.get("generated_at", "")

    portfolio_summary = portfolio.get("summary", {})
    report_summary = report.get("summary", {})
    candidates = scan.get("candidates", [])[-25:]  # most recent 25
    positions = portfolio.get("positions", [])
    performance = report.get("performance", {})

    # Realized P/L: prefer the authoritative ledger value
    # (portfolio_summary.realized_pnl); fall back to computing from
    # strategy_results JSONL exits only if the ledger doesn't have it.
    exits = [e for e in strategy_results if e.get("event") == "exit"]
    ledger_realized = portfolio_summary.get("realized_pnl")
    if ledger_realized is not None:
        try:
            realized_pnl = float(ledger_realized)
        except (TypeError, ValueError):
            realized_pnl = sum(float(e.get("pnl", 0)) for e in exits)
    else:
        realized_pnl = sum(float(e.get("pnl", 0)) for e in exits)
    realized_wins = [e for e in exits if float(e.get("pnl", 0)) > 0]
    realized_losses = [e for e in exits if float(e.get("pnl", 0)) <= 0]
    realized_win_rate = (
        len(realized_wins) / len(exits) if exits else 0.0
    )

    # Net P/L: prefer report summary; fall back to realized + unrealized
    net_pnl = report_summary.get("net_pnl")
    if net_pnl is None:
        try:
            net_pnl = realized_pnl + float(portfolio_summary.get("unrealized_pnl", 0))
        except (TypeError, ValueError):
            net_pnl = None

    # Circuit breaker metrics
    circuit_breaker = snapshot.get("circuit_breaker", {})
    consecutive_losses = circuit_breaker.get("consecutive_losses", 0)
    drawdown_current = circuit_breaker.get("drawdown_current_pct")
    drawdown_max = circuit_breaker.get("drawdown_max_pct")
    cb_label = "Consecutive Losses"
    cb_value = str(consecutive_losses)
    cb_raw = False
    if consecutive_losses >= 5:
        cb_value = f'<span style="color:#f85149">{consecutive_losses} 🔥</span>'
        cb_raw = True
    elif consecutive_losses >= 3:
        cb_value = f'<span style="color:#d29922">{consecutive_losses} ⚠️</span>'
        cb_raw = True

    kill_banner = (
        '<div class="banner kill-active" id="kill-banner" role="alert">KILL SWITCH ACTIVE - trading halted '
        '<button class="btn" onclick="toggleKillSwitch(\'resume\')" aria-label="Resume all trading activity">Resume Trading</button></div>'
        if kill.get("active")
        else '<div class="banner kill-inactive" id="kill-banner" role="status">Kill switch: inactive (trading enabled) '
             '<button class="btn kill-halt" onclick="toggleKillSwitch(\'halt\')" aria-label="Halt all trading activity">HALT Trading</button></div>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <title>Autonomous Trading Agent - Live</title>
  <style>
    body {{ margin: 24px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: #101418; color: #e6edf3; }}
    h1, h2 {{ margin: 0 0 12px; }}
    h2 {{ margin-top: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 16px 0; }}
    .card {{ background: #18202a; border: 1px solid #2f3b48; border-radius: 10px; padding: 14px; }}
    .label {{ color: #8b949e; font-size: 12px; }}
    .value {{ font-size: 24px; margin-top: 4px; }}
    .value.positive {{ color: #3fb950; }}
    .value.negative {{ color: #f85149; }}
    .banner {{ padding: 8px 12px; border-radius: 6px; margin: 12px 0; font-weight: bold; }}
    .kill-active {{ background: #f85149; color: #fff; }}
    .kill-inactive {{ background: #18202a; color: #8b949e; border: 1px solid #2f3b48; }}
    table {{ width: 100%; border-collapse: collapse; margin: 8px 0 20px; }}
    th, td {{ border-bottom: 1px solid #2f3b48; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ color: #8b949e; }}
    .GREEN, .APPROVED, .FILLED {{ color: #3fb950; }}
    .YELLOW {{ color: #d29922; }}
    .REJECTED, .NO_SIGNAL {{ color: #f85149; }}
    .timestamp {{ color: #8b949e; margin-bottom: 16px; font-size: 12px; }}
    .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    @media (max-width: 900px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; background: #18202a; }}
    .feed {{ max-height: 480px; overflow-y: auto; }}
    .btn {{ cursor: pointer; padding: 6px 14px; border: none; border-radius: 6px; font-weight: bold; font-size: 13px; margin-left: 12px; transition: opacity 0.2s, outline 0.2s; }}
    .btn:hover:not(:disabled) {{ opacity: 0.85; }}
    .btn:disabled {{ cursor: not-allowed; opacity: 0.5; }}
    .btn:focus-visible {{ outline: 2px solid #58a6ff; outline-offset: 2px; }}
    .kill-halt {{ background: #f85149; color: #fff; }}
    #kill-banner button {{ background: #238636; color: #fff; }}
    .pnl-positive {{ color: #3fb950; }}
    .pnl-negative {{ color: #f85149; }}
    .regime-badge {{ display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: bold; margin: 4px 0; }}
    .regime-high_volatility {{ background: #f85149; color: #fff; }}
    .regime-strong_uptrend {{ background: #3fb950; color: #fff; }}
    .regime-weak_uptrend {{ background: #238636; color: #fff; }}
    .regime-range_bound {{ background: #d29922; color: #000; }}
    .regime-weak_downtrend {{ background: #a45e00; color: #fff; }}
    .regime-strong_downtrend {{ background: #8b0000; color: #fff; }}
    .regime-metrics {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 12px; }}
    .regime-metric {{ background: #0d1117; padding: 8px; border-radius: 6px; text-align: center; }}
    .regime-metric .label {{ font-size: 11px; color: #8b949e; }}
    .regime-metric .value {{ font-size: 16px; margin-top: 4px; }}
    .regime-explanation {{ font-size: 12px; color: #8b949e; margin-top: 8px; line-height: 1.5; }}
    .strategy-card {{ background: #18202a; border: 1px solid #2f3b48; border-radius: 10px; padding: 16px; margin: 16px 0; }}
    .strategy-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
    .strategy-name {{ font-size: 16px; font-weight: bold; color: #e6edf3; }}
    .strategy-badge {{ padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: bold; }}
    .strategy-badge-win {{ background: #3fb950; color: #fff; }}
    .strategy-badge-loss {{ background: #f85149; color: #fff; }}
    .strategy-badge-neutral {{ background: #8b949e; color: #000; }}
    .strategy-metrics {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-top: 12px; }}
    .strategy-metric {{ text-align: center; padding: 8px; background: #0d1117; border-radius: 6px; }}
    .strategy-metric .label {{ font-size: 11px; color: #8b949e; }}
    .strategy-metric .value {{ font-size: 18px; margin-top: 4px; font-weight: bold; }}
    .strategy-pnl {{ font-size: 20px; }}
    .strategy-allocation {{ font-size: 14px; padding: 4px 8px; border-radius: 4px; }}
    .allocation-full {{ background: #3fb950; color: #fff; }}
    .allocation-half {{ background: #d29922; color: #000; }}
    .allocation-skip {{ background: #f85149; color: #fff; }}
  </style>
</head>
<body>
  <h1>Autonomous Trading Agent</h1>
  <div class="timestamp">Live dashboard - auto-refresh 5s - generated {html.escape(str(generated_at))}</div>
  {kill_banner}

  <section class="grid">
    {_card("Equity", _money(portfolio_summary.get("equity")))}
    {_card("Cash", _money(portfolio_summary.get("cash")))}
    {_card("Exposure", _pct(portfolio_summary.get("exposure")))}
    {_card("Unrealized P/L", _signed_money(portfolio_summary.get("unrealized_pnl")), raw=True)}
    {_card("Realized P/L", _signed_money(realized_pnl), raw=True)}
    {_card("Net P/L", _signed_money(net_pnl), raw=True)}
    {_card("Open Positions", portfolio_summary.get("positions", 0))}
    {_card("Realized Win Rate", _pct(realized_win_rate))}
    {_card(cb_label, cb_value, raw=cb_raw)}
    {_card("Drawdown (max)", f"{drawdown_max:.2f}%" if drawdown_max is not None else "n/a")}
    {_card("Drawdown (current)", f"{drawdown_current:.2f}%" if drawdown_current is not None else "n/a")}
  </section>

  {_market_regime_widget(snapshot.get("market_regime"))}
  {_strategy_attribution_widget(snapshot.get("strategy_attribution"))}

  <div class="two-col">
    <div>
      <h2>Open Positions</h2>
      {_positions_table(positions)}
      <h2>Recent Scan Candidates</h2>
      {_table(candidates, ["ticker", "status", "quality", "confidence", "entry"])}
    </div>
    <div>
      <h2>Decision Feed (live)</h2>
      <div class="feed">
      {_decision_feed(decisions[-50:])}
      </div>
      <h2>Closed Positions</h2>
      {_closed_positions_table(exits[-20:])}
    </div>
  </div>

  <script>
  async function toggleKillSwitch(action) {{
    const btn = document.querySelector('#kill-banner button');
    let originalText = '';
    if (btn) {{
      originalText = btn.innerHTML;
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      btn.innerHTML = action === 'halt' ? 'Halting... ⏳' : 'Resuming... ⏳';
    }}
    try {{
      const resp = await fetch('/api/kill-switch', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{action: action, reason: 'dashboard ' + action}})
      }});
      const data = await resp.json();
      if (data.success) {{
        location.reload();
      }} else {{
        alert('Failed: ' + (data.error || 'unknown error'));
        if (btn) {{ btn.disabled = false; btn.innerHTML = originalText; }}
      }}
    }} catch (e) {{
      alert('Network error: ' + e.message);
      if (btn) {{ btn.disabled = false; btn.innerHTML = originalText; }}
    }}
  }}
  </script>
</body>
</html>
"""


def _decision_feed(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return '<p class="label">No decisions yet.</p>'
    rows = []
    for e in reversed(entries):  # newest first
        cmd = html.escape(str(e.get("command", "")))
        ticker = html.escape(str(e.get("ticker", "")))
        raw_status = str(e.get("status", ""))
        # Sanitize status for use as CSS class - only allow alphanumeric and hyphens
        status_class = "".join(c for c in raw_status if c.isalnum() or c == "-")
        status = html.escape(raw_status)
        reason = html.escape(str(e.get("reason", "")))
        extra = ""
        if e.get("confidence") is not None:
            extra += f' <span class="badge">conf={html.escape(str(e["confidence"]))}</span>'
        if e.get("fill_price") is not None:
            extra += f' <span class="badge">@${html.escape(str(e["fill_price"]))}</span>'
        rows.append(
            f'<tr><td class="{status_class}">{cmd}</td><td>{ticker}</td>'
            f'<td class="{status_class}">{status}</td><td>{reason}{extra}</td></tr>'
        )
    return (
        '<table><thead><tr><th>Command</th><th>Ticker</th><th>Status</th>'
        f'<th>Detail</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )


def _positions_table(positions: list[dict[str, Any]]) -> str:
    """Render an enhanced positions table with live P/L and risk metrics."""
    if not positions:
        return '<p class="label">No open positions.</p>'

    headers = ["Ticker", "Qty", "Cost", "Last", "Unrealized", "U.Spread%", "To Stop%", "To Target%", "Stop", "Target"]
    rows = []
    for pos in positions:
        ticker = html.escape(str(pos.get("ticker", "")))
        qty = pos.get("quantity", 0)
        cost = pos.get("average_cost", 0.0)
        last = pos.get("last_price")
        unrealized = pos.get("unrealized_pnl")
        unrealized_pct = pos.get("unrealized_pct")
        dist_stop = pos.get("dist_to_stop")
        dist_target = pos.get("dist_to_target")
        stop = pos.get("stop_loss")
        target = pos.get("profit_target")

        last_str = f"${last:.2f}" if last is not None else "—"

        if unrealized is not None:
            pnl_class = "pnl-positive" if unrealized >= 0 else "pnl-negative"
            unrealized_str = f'<span class="{pnl_class}">${unrealized:.2f}</span>'
        else:
            unrealized_str = "—"

        if unrealized_pct is not None:
            pnl_class = "pnl-positive" if unrealized_pct >= 0 else "pnl-negative"
            pct_str = f'<span class="{pnl_class}">{unrealized_pct:+.2f}%</span>'
        else:
            pct_str = "—"

        dist_stop_str = f"{dist_stop:.2f}%" if dist_stop is not None else "—"
        dist_target_str = f"{dist_target:.2f}%" if dist_target is not None else "—"
        stop_str = f"${stop:.2f}" if stop is not None else "—"
        target_str = f"${target:.2f}" if target is not None else "—"

        rows.append(
            f"<tr><td>{ticker}</td><td>{qty}</td><td>${cost:.2f}</td>"
            f"<td>{last_str}</td><td>{unrealized_str}</td><td>{pct_str}</td>"
            f"<td>{dist_stop_str}</td><td>{dist_target_str}</td>"
            f"<td>{stop_str}</td><td>{target_str}</td></tr>"
        )

    return (
        '<table><thead><tr>' +
        ''.join(f'<th>{h}</th>' for h in headers) +
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )


def _market_regime_widget(regime: dict | None) -> str:
    """Render market regime widget showing current conditions and why symbols may be blocked."""
    if not regime:
        return ''
    
    regime_name = regime.get("regime", "unknown")
    regime_class = f"regime-{regime_name}"
    regime_display = regime_name.replace("_", " ").title()
    
    # Determine if regime is favorable or not
    favorable = regime_name in ["strong_uptrend", "weak_uptrend", "range_bound"]
    status_color = "#3fb950" if favorable else "#f85149"
    status_text = "FAVORABLE" if favorable else "UNFAVORABLE"
    
    # Build explanation
    explanations = []
    if regime_name == "high_volatility":
        explanations.append(f"⚠️ High volatility detected - V3 strategy reduces risk by avoiding trades")
        explanations.append(f"  • ADX {regime.get('adx', 0):.0f} indicates {'strong trend' if regime.get('adx', 0) > 25 else 'weak trend'}")
        explanations.append(f"  • Volatility at {regime.get('volatility_percentile', 0):.0f}th percentile vs recent history")
    elif regime_name == "strong_downtrend":
        explanations.append(f"⚠️ Strong downtrend - V3 strategy avoids counter-trend trades")
        explanations.append(f"  • Price {regime.get('price_vs_sma50', 0):.1f}% below 50-day SMA")
        explanations.append(f"  • Momentum: {regime.get('momentum', 0):.2f}")
    elif regime_name == "range_bound":
        explanations.append(f"✓ Range-bound market - mean reversion strategies preferred")
        explanations.append(f"  • Price {regime.get('price_vs_ema20', 0):.1f}% from 20-day EMA")
    elif "uptrend" in regime_name:
        explanations.append(f"✓ Uptrend detected - trend-following strategies active")
        explanations.append(f"  • Price {regime.get('price_vs_sma50', 0):.1f}% above 50-day SMA")
    
    explanation_html = "<br/>".join(f'<div>{e}</div>' for e in explanations)
    
    return f'''
  <div class="card" style="margin: 16px 0; border-left: 4px solid {status_color};">
    <div class="label">Market Regime (SPY)</div>
    <div style="margin: 8px 0;">
      <span class="regime-badge {regime_class}">{regime_display}</span>
      <span style="color: {status_color}; margin-left: 12px; font-weight: bold;">{status_text} FOR TRADING</span>
    </div>
    <div class="regime-metrics">
      <div class="regime-metric">
        <div class="label">ADX (Trend)</div>
        <div class="value">{regime.get("adx", 0):.1f}</div>
      </div>
      <div class="regime-metric">
        <div class="label">Volatility</div>
        <div class="value">{regime.get("volatility_percentile", 0):.0f}th %</div>
      </div>
      <div class="regime-metric">
        <div class="label">vs SMA50</div>
        <div class="value">{regime.get("price_vs_sma50", 0):+.1f}%</div>
      </div>
      <div class="regime-metric">
        <div class="label">vs EMA20</div>
        <div class="value">{regime.get("price_vs_ema20", 0):+.1f}%</div>
      </div>
      <div class="regime-metric">
        <div class="label">Momentum</div>
        <div class="value">{regime.get("momentum", 0):.2f}</div>
      </div>
      <div class="regime-metric">
        <div class="label">Recommendation</div>
        <div class="value" style="font-size: 14px;">{"Trade" if favorable else "Wait"}</div>
      </div>
    </div>
    <div class="regime-explanation">
      {explanation_html}
    </div>
  </div>
'''


def _strategy_attribution_widget(attribution: list[dict] | None) -> str:
    """Render strategy performance attribution widget."""
    if not attribution:
        return '<div class="card" style="margin: 16px 0;"><div class="label">Strategy Attribution</div><p class="label">No strategy data yet. Trades will appear here after exits.</p></div>'
    
    # Sort by recent_net_pnl descending
    sorted_strategies = sorted(attribution, key=lambda x: x.get('recent_net_pnl', 0), reverse=True)
    
    rows = []
    for strat in sorted_strategies:
        name = html.escape(str(strat.get('strategy', 'Unknown')))
        win_rate = strat.get('recent_win_rate', 0)
        net_pnl = strat.get('recent_net_pnl', 0)
        total_exits = strat.get('total_exits', 0)
        recent_exits = strat.get('recent_exits', 0)
        allocation = strat.get('allocation', 1.0)
        wins = strat.get('recent_wins', 0)
        losses = strat.get('recent_losses', 0)
        
        # Badge based on win rate
        if win_rate >= 0.5:
            badge_class = "strategy-badge-win"
            badge_text = f"✓ {win_rate*100:.0f}% WR"
        elif win_rate >= 0.4:
            badge_class = "strategy-badge-neutral"
            badge_text = f"~ {win_rate*100:.0f}% WR"
        else:
            badge_class = "strategy-badge-loss"
            badge_text = f"✗ {win_rate*100:.0f}% WR"
        
        # Allocation badge
        if allocation >= 1.0:
            alloc_class = "allocation-full"
            alloc_text = "100%"
        elif allocation >= 0.5:
            alloc_class = "allocation-half"
            alloc_text = "50%"
        else:
            alloc_class = "allocation-skip"
            alloc_text = "SKIP"
        
        # P&L color
        pnl_class = "pnl-positive" if net_pnl >= 0 else "pnl-negative"
        pnl_sign = "+" if net_pnl >= 0 else ""
        
        rows.append(f'''
  <div class="strategy-card">
    <div class="strategy-header">
      <span class="strategy-name">{name}</span>
      <span class="strategy-badge {badge_class}">{badge_text}</span>
    </div>
    <div class="strategy-metrics">
      <div class="strategy-metric">
        <div class="label">Net P&L</div>
        <div class="value strategy-pnl {pnl_class}">{pnl_sign}${net_pnl:.2f}</div>
      </div>
      <div class="strategy-metric">
        <div class="label">Wins / Losses</div>
        <div class="value">{wins} / {losses}</div>
      </div>
      <div class="strategy-metric">
        <div class="label">Total Exits</div>
        <div class="value">{total_exits}</div>
      </div>
      <div class="strategy-metric">
        <div class="label">Recent (n={recent_exits})</div>
        <div class="value">{win_rate*100:.0f}%</div>
      </div>
      <div class="strategy-metric">
        <div class="label">Allocation</div>
        <div class="value"><span class="strategy-allocation {alloc_class}">{alloc_text}</span></div>
      </div>
    </div>
  </div>
''')
    
    return f'''
  <div style="margin: 24px 0;">
    <h2>Strategy Performance Attribution</h2>
    <p class="label">Last {50} exits per strategy</p>
    {''.join(rows)}
  </div>
'''


def _closed_positions_table(exits: list[dict[str, Any]]) -> str:
    """Render a table of closed positions with P&L details."""
    if not exits:
        return '<p class="label">No closed positions yet.</p>'
    
    headers = ["Ticker", "Entry $", "Exit $", "Qty", "P&L", "Result", "Reason"]
    rows = []
    for exit_event in reversed(exits):  # newest first
        ticker = html.escape(str(exit_event.get("ticker", "")))
        entry_price = exit_event.get("entry_price")
        exit_price = exit_event.get("exit_price")
        quantity = exit_event.get("quantity", 0)
        pnl = exit_event.get("pnl", 0)
        win = exit_event.get("win", False)
        reason = html.escape(str(exit_event.get("reason", "")))
        
        entry_str = f"${entry_price:.2f}" if entry_price else "—"
        exit_str = f"${exit_price:.2f}" if exit_price else "—"
        
        if pnl is not None:
            pnl_class = "pnl-positive" if pnl >= 0 else "pnl-negative"
            pnl_str = f'<span class="{pnl_class}">${pnl:.2f}</span>'
        else:
            pnl_str = "—"
        
        result_str = '<span style="color:#3fb950">✓ WIN</span>' if win else '<span style="color:#f85149">✗ LOSS</span>'
        
        rows.append(
            f"<tr><td>{ticker}</td><td>{entry_str}</td><td>{exit_str}</td>"
            f"<td>{quantity}</td><td>{pnl_str}</td><td>{result_str}</td><td>{reason}</td></tr>"
        )
    
    return (
        '<table><thead><tr>' +
        ''.join(f'<th>{h}</th>' for h in headers) +
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )


def _toggle_kill_switch(settings: Settings, action: str, reason: str) -> dict[str, object]:
    """Toggle the kill switch via the portfolio ledger."""
    try:
        from trading_bot.portfolio.ledger import PortfolioLedger
        from trading_bot.safety.kill_switch import halt_trading, resume_trading

        ledger = PortfolioLedger(Path(settings.app.state_db_path))
        if action == "halt":
            halt_trading(ledger, reason=reason, triggered_by="dashboard")
            return {"success": True, "action": "halt", "message": "Trading halted"}
        elif action == "resume":
            resume_trading(ledger, resumed_by="dashboard")
            return {"success": True, "action": "resume", "message": "Trading resumed"}
        else:
            return {"success": False, "error": f"Unknown action: {action}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _get_cached_price(ticker: str) -> float | None:
    """Get cached price if still valid (within TTL)."""
    if ticker not in _price_cache_timestamps:
        return None
    age = time.time() - _price_cache_timestamps[ticker]
    if age > _CACHE_TTL_SECONDS:
        # Expired - remove from cache
        _price_cache.pop(ticker, None)
        _price_cache_timestamps.pop(ticker, None)
        return None
    return _price_cache.get(ticker, (None, None))[0] if _price_cache.get(ticker) else None


def _set_cached_price(ticker: str, price: float) -> None:
    """Cache a price with timestamp."""
    global _price_cache, _price_cache_timestamps
    # Evict oldest if at capacity
    if len(_price_cache) >= _CACHE_MAX_SIZE:
        oldest = next(iter(_price_cache))
        _price_cache.pop(oldest, None)
        _price_cache_timestamps.pop(oldest, None)
    _price_cache[ticker] = (price, time.time())
    _price_cache_timestamps[ticker] = time.time()


def _enrich_positions(positions: list[dict[str, Any]], settings: Settings) -> list[dict[str, Any]]:
    """Enrich position rows with live price data and computed metrics.

    Computes unrealized P/L ($ and %), distance to stop (%), and distance to
    target (%) by fetching the latest intraday bar for each ticker. Uses a
    30-second TTL cache to avoid redundant API calls on rapid refreshes.
    When a fresh price cannot be fetched, falls back to stop-loss (worst-case),
    then average_cost (neutral) so the dashboard always has a price to show.

    Safe to call during snapshot — errors for individual tickers are swallowed
    and a best-effort row is still returned.
    """
    if not positions:
        return positions

    try:
        from trading_bot.data import market_data
    except Exception:
        return positions

    enriched = []
    for pos in positions:
        ticker = pos.get("ticker", "")
        quantity = pos.get("quantity", 0)
        avg_cost = pos.get("average_cost", 0.0)
        stop = pos.get("stop_loss")
        target = pos.get("profit_target")

        # Try cache first
        last_price = _get_cached_price(ticker)

        # Cache miss - fetch from market data
        if last_price is None:
            try:
                frame = market_data.fetch_bars(
                    ticker,
                    settings.market_data.intraday_period,
                    settings.market_data.intraday_interval,
                )
                if not frame.empty and "close" in frame.columns:
                    last_price = float(frame.iloc[-1]["close"])
                    _set_cached_price(ticker, last_price)
            except Exception as e:
                logger.debug(f"Failed to fetch price for {ticker}: {e}")

        # Fail-closed fallback chain: stop-loss → average_cost
        if last_price is None:
            if stop is not None and stop > 0:
                last_price = stop
            elif avg_cost > 0:
                last_price = avg_cost

        # Only compute metrics if we have valid data
        if last_price is not None and last_price > 0 and avg_cost > 0 and quantity > 0:
            unrealized = (last_price - avg_cost) * quantity
            unrealized_pct = ((last_price - avg_cost) / avg_cost) * 100
            pos["last_price"] = round(last_price, 2)
            pos["unrealized_pnl"] = round(unrealized, 2)
            pos["unrealized_pct"] = round(unrealized_pct, 2)

            if stop is not None and stop > 0 and last_price > 0:
                dist_to_stop = ((last_price - stop) / last_price) * 100
                pos["dist_to_stop"] = round(dist_to_stop, 2)
            if target is not None and target > 0 and last_price > 0:
                dist_to_target = ((target - last_price) / last_price) * 100
                pos["dist_to_target"] = round(dist_to_target, 2)

        enriched.append(pos)

    return enriched


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
    performance = report.get("performance", {})

    total_trades = performance.get("total_trades", 0)
    wins = performance.get("winning_trades", 0)
    losses = performance.get("losing_trades", 0)
    win_rate = performance.get("win_rate", 0)
    avg_win = performance.get("avg_win", 0)
    avg_loss = performance.get("avg_loss", 0)
    profit_factor = performance.get("profit_factor", 0)
    sharpe = performance.get("sharpe_ratio", 0)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Autonomous Trading Agent</title>
  <style>
    body {{ margin: 24px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: #101418; color: #e6edf3; }}
    h1, h2 {{ margin: 0 0 12px; }}
    h2 {{ margin-top: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 16px 0; }}
    .card {{ background: #18202a; border: 1px solid #2f3b48; border-radius: 10px; padding: 14px; }}
    .label {{ color: #8b949e; font-size: 12px; }}
    .value {{ font-size: 24px; margin-top: 4px; }}
    .value.positive {{ color: #3fb950; }}
    .value.negative {{ color: #f85149; }}
    table {{ width: 100%; border-collapse: collapse; margin: 8px 0 20px; }}
    th, td {{ border-bottom: 1px solid #2f3b48; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ color: #8b949e; }}
    .GREEN {{ color: #3fb950; }}
    .YELLOW {{ color: #d29922; }}
    .REJECTED, .NO_SIGNAL {{ color: #f85149; }}
    .chart-container {{ background: #18202a; border: 1px solid #2f3b48; border-radius: 10px; padding: 20px; margin: 16px 0; }}
    .chart-row {{ display: flex; gap: 20px; flex-wrap: wrap; }}
    .chart-box {{ flex: 1; min-width: 300px; }}
    .bar-container {{ display: flex; height: 30px; border-radius: 5px; overflow: hidden; margin-top: 10px; }}
    .bar-win {{ background: #3fb950; }}
    .bar-loss {{ background: #f85149; }}
    .stats-row {{ display: flex; gap: 20px; margin-top: 10px; flex-wrap: wrap; }}
    .stat-box {{ background: #0d1117; padding: 10px 15px; border-radius: 5px; flex: 1; min-width: 120px; }}
    .stat-label {{ color: #8b949e; font-size: 11px; }}
    .stat-value {{ font-size: 18px; margin-top: 2px; }}
    .gauge-container {{ width: 150px; height: 80px; position: relative; margin: 10px auto; }}
    .gauge-bg {{ fill: #21262d; }}
    .gauge-fill {{ fill: url(#gaugeGradient); }}
    .gauge-text {{ text-anchor: middle; dominant-baseline: middle; font-size: 24px; fill: #e6edf3; }}
    .pnl-distribution {{ display: flex; align-items: flex-end; height: 100px; gap: 4px; margin-top: 10px; justify-content: center; }}
    .pnl-bar {{ width: 40px; background: #388bfd; border-radius: 3px 3px 0 0; position: relative; min-height: 5px; }}
    .pnl-bar.positive {{ background: #3fb950; }}
    .pnl-bar.negative {{ background: #f85149; }}
    .pnl-label {{ position: absolute; bottom: -20px; left: 50%; transform: translateX(-50%); font-size: 10px; white-space: nowrap; }}
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

  {_render_performance_section(performance, total_trades, wins, losses, win_rate, avg_win, avg_loss, profit_factor, sharpe)}

  <h2>Scan Candidates</h2>
  {_table(candidates, ["ticker", "status", "quality", "reason", "confidence", "entry", "stop", "target"])}
  <h2>Positions</h2>
  {_table(positions, ["ticker", "quantity", "average_cost", "last_price", "market_value", "unrealized_pnl", "allocation"])}
  <h2>Recent Decisions</h2>
  {_table(decisions, ["timestamp", "command", "ticker", "status", "reason"])}
</body>
</html>
"""


def _card(label: str, value: object, raw: bool = False) -> str:
    """Render a dashboard card.
    
    Args:
        label: Card label (always HTML-escaped)
        value: Card value (escaped unless raw=True)
        raw: If True, value is treated as safe HTML. ONLY use with trusted
            sources like f-strings built in this module. NEVER use with
            user input or external data.
    """
    escaped = str(value) if raw else html.escape(str(value))
    return f'<div class="card"><div class="label">{html.escape(label)}</div><div class="value">{escaped}</div></div>'


def _table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return '<p class="label">No rows.</p>'
    head = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
    
    def _sanitize_class(value: str) -> str:
        """Sanitize string for use as CSS class - only alphanumeric and hyphens."""
        return "".join(c for c in str(value) if c.isalnum() or c == "-")
    
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="{_sanitize_class(row.get(column, ""))}">{html.escape(str(row.get(column, "")))}</td>'
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


def _signed_money(value: object) -> str:
    try:
        v = float(value)
        sign = "+" if v >= 0 else ""
        cls = "positive" if v >= 0 else "negative"
        return f'<span class="{cls}">{sign}${v:,.2f}</span>'
    except (TypeError, ValueError):
        return "n/a"


def _pct(value: object) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "n/a"


def _render_performance_section(
    performance: dict[str, Any],
    total_trades: int,
    wins: int,
    losses: int,
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    profit_factor: float,
    sharpe: float,
) -> str:
    """Render the performance section with charts."""
    if total_trades == 0:
        return '<h2>Performance Metrics</h2><p class="label">No trades yet.</p>'

    win_pct = (wins / total_trades * 100) if total_trades > 0 else 0
    loss_pct = (losses / total_trades * 100) if total_trades > 0 else 0

    return f"""
  <h2>Performance Metrics</h2>
  <div class="chart-container">
    <div class="chart-row">
      <div class="chart-box">
        <div class="label" id="trade-dist-label">Trade Distribution</div>
        <div class="bar-container" role="img" aria-label="Trade Distribution: {wins} wins and {losses} losses">
          <div class="bar-win" style="width: {win_pct:.1f}%" title="{wins} wins ({win_pct:.1f}%)"></div>
          <div class="bar-loss" style="width: {loss_pct:.1f}%" title="{losses} losses ({loss_pct:.1f}%)"></div>
        </div>
        <div style="margin-top: 8px; font-size: 12px;">
          <span style="color: #3fb950;">● {wins} Wins</span>
          <span style="color: #f85149; margin-left: 15px;">● {losses} Losses</span>
        </div>
      </div>

      <div class="chart-box">
        <div class="label" id="win-rate-label">Win Rate Gauge</div>
        <div class="gauge-container" role="img" aria-label="Win Rate Gauge: {win_rate:.1%}">
          <svg viewBox="0 0 150 80" aria-hidden="true">
            <defs>
              <linearGradient id="gaugeGradient" x1="0%" y1="0%" x2="100%" y2="0%">
                <stop offset="0%" style="stop-color:#f85149"/>
                <stop offset="50%" style="stop-color:#d29922"/>
                <stop offset="100%" style="stop-color:#3fb950"/>
              </linearGradient>
            </defs>
            <path d="M 10 70 A 65 65 0 0 1 140 70" fill="none" stroke="#21262d" stroke-width="15"/>
            <path d="M 10 70 A 65 65 0 0 1 {_gauge_end_x(win_rate)} {_gauge_end_y(win_rate)}"
                  fill="none" stroke="url(#gaugeGradient)" stroke-width="15"
                  stroke-dasharray="{win_rate * 2.04:.1f} 204"/>
            <text x="75" y="55" class="gauge-text">{win_rate:.1%}</text>
          </svg>
        </div>
      </div>

      <div class="chart-box">
        <div class="label" id="avg-pnl-label">Average Trade P&L</div>
        <div class="pnl-distribution" role="img" aria-label="Average Trade P&L: win is {_money(avg_win)}, loss is {_money(avg_loss)}">
          <div class="pnl-bar positive" style="height: {min(abs(avg_win) * 5, 100):.0f}px">
            <div class="pnl-label" aria-hidden="true">Win</div>
          </div>
          <div class="pnl-bar negative" style="height: {min(abs(avg_loss) * 5, 100):.0f}px">
            <div class="pnl-label" aria-hidden="true">Loss</div>
          </div>
        </div>
        <div style="text-align: center; margin-top: 25px; font-size: 12px;">
          <span style="color: #3fb950;">Avg Win: {_money(avg_win)}</span>
          <span style="color: #f85149; margin-left: 15px;">Avg Loss: {_money(avg_loss)}</span>
        </div>
      </div>
    </div>

    <div class="stats-row">
      <div class="stat-box">
        <div class="stat-label">Total Trades</div>
        <div class="stat-value">{total_trades}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Profit Factor</div>
        <div class="stat-value {_value_class(profit_factor, 1.0)}">{profit_factor:.2f}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Sharpe Ratio</div>
        <div class="stat-value {_value_class(sharpe, 1.0)}">{sharpe:.2f}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Largest Win</div>
        <div class="stat-value positive">{_money(performance.get('largest_win', 0))}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Largest Loss</div>
        <div class="stat-value negative">{_money(performance.get('largest_loss', 0))}</div>
      </div>
    </div>
  </div>
"""


def _gauge_end_x(win_rate: float) -> float:
    """Calculate gauge needle end X coordinate."""
    import math
    angle = math.pi * (1 - win_rate)  # 0% = left (pi), 100% = right (0)
    return 75 + 65 * math.cos(angle)


def _gauge_end_y(win_rate: float) -> float:
    """Calculate gauge needle end Y coordinate."""
    import math
    angle = math.pi * (1 - win_rate)
    return 70 - 65 * math.sin(angle)


def _value_class(value: float, threshold: float) -> str:
    """Return CSS class based on value vs threshold."""
    if value >= threshold:
        return "positive"
    return "negative"


def _read_json(path: str) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.exists():
        return {}
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _read_jsonl_tail(path: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return the last `limit` JSON objects from a JSONL file.

    Uses a bounded tail read to avoid loading multi-MB files fully on each
    refresh. Malformed lines are skipped (never raise).
    """
    candidate = Path(path)
    if not candidate.exists():
        return []
    try:
        with candidate.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            # Read up to ~64KB from the tail — enough for 50 lines of our schema
            read_size = min(size, 65536)
            fh.seek(size - read_size)
            tail = fh.read(read_size).decode("utf-8", errors="replace")
        # First line may be partial; drop it
        lines = tail.splitlines()
        if read_size < size and lines:
            lines = lines[1:]
        records: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records[-limit:]
    except OSError:
        return []


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

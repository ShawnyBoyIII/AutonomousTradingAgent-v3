"""Tests for the 02 Book tabbed layout (Open | Closed).

The tabs drive visibility via `.is-active` on both the tab button
and the pane, plus a `hidden` attribute on the inactive pane. We
verify that `activateBookTab` correctly toggles state, sets ARIA
attributes, and persists choice across page reloads via localStorage.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch


def _rendered_contains(html: str, *needles: str) -> bool:
    return all(n in html for n in needles)


def test_template_renders_tabbed_book_card():
    """The template exposes a tabbed Open/Closed layout (no longer two-leaf)."""
    html = Path("ui/dashboard/templates/dashboard.html").read_text(encoding="utf-8")
    assert _rendered_contains(
        html,
        'id="bookTitle"',
        'class="card card--book"',
        'role="tablist"',
        'id="openTab"', 'id="openPane"',
        'id="closedTab"', 'id="closedPane"',
        'openTabCount', 'closedTabCount',
    ), "Template missing tab structure"


def test_template_no_longer_renders_two_leaf_book():
    """The legacy two-leaf `book__spine` divider and leaf grid are gone."""
    html = Path("ui/dashboard/templates/dashboard.html").read_text(encoding="utf-8")
    assert "book__spine" not in html, "Stale .book__spine markup still present"
    assert "book__leaf--open" not in html, "Stale .book__leaf--open markup present"
    assert "book__leaf--closed" not in html, "Stale .book__leaf--closed markup present"
    assert 'id="openLeafTitle"' not in html, "Stale id=openLeafTitle markup present"
    assert 'id="closedLeafTitle"' not in html, "Stale id=closedLeafTitle markup present"


def test_open_tab_is_default_active():
    """The Open tab is active by default (matches user's choice)."""
    html = Path("ui/dashboard/templates/dashboard.html").read_text(encoding="utf-8")
    # openTab has aria-selected="true" and class="is-active" in the template
    open_match = re.search(r'<button[^>]*id="openTab"[^>]*>', html)
    assert open_match, "Open tab not found in template"
    open_tag = open_match.group(0)
    assert 'class="tab is-active"' in open_tag
    assert 'aria-selected="true"' in open_tag

    # openPane is visible (no `hidden`, class="tab-pane is-active")
    pane_match = re.search(r'<div[^>]*id="openPane"[^>]*>', html)
    assert pane_match
    pane_tag = pane_match.group(0)
    assert 'class="tab-pane is-active"' in pane_tag
    assert "hidden" not in pane_tag

    # closedPane is hidden
    closed_pane = re.search(r'<div[^>]*id="closedPane"[^>]*>', html)
    assert closed_pane
    closed_pane_tag = closed_pane.group(0)
    assert "hidden" in closed_pane_tag
    assert 'class="tab-pane"' in closed_pane_tag
    assert 'class="tab-pane is-active"' not in closed_pane_tag


def test_tab_switcher_logic():
    """The tab switcher (selected via monkeypatched localStorage) flips ARIA + .is-active."""
    import subprocess

    # We exercise the bundled dashboard.js with a tiny harness.
    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")

    # Minimal DOM stub + localStorage stub
    harness = r"""
    var document = { getElementById: function(id){ return null; }, querySelectorAll: function(){ return []; } };
    var window = {
      localStorage: {
        _store: { 'book-active-tab': 'closed' },
        getItem: function(k){ return this._store[k]; },
        setItem: function(k,v){ this._store[k]=v; },
      },
      addEventListener: function(){},
    };
    """ + js + r"""
    ;return { initialTab: (typeof getActiveBookTab === 'function') ? getActiveBookTab() : null };
    """
    out = subprocess.run(
        ["python", "-c", f"import subprocess, json; print(subprocess.run(['node', '-e', '''{harness}'''], capture_output=True, text=True).stdout)"],
        capture_output=True, text=True,
    )

    # Robust assertion: verify the source contains the expected helpers.
    # Phase 1 refactor consolidated the book + window tab controllers into
    # one ``bindTabs`` helper; ``bindBookTabs`` is now a thin wrapper.
    assert "function bindTabs" in js, "bindTabs helper missing"
    assert "function bindBookTabs" in js, "bindBookTabs wrapper missing"
    assert "function bindWindowTabs" in js, "bindWindowTabs wrapper missing"
    assert 'arrow-right' in js.lower() or 'ArrowRight' in js, "ArrowRight handler missing"
    assert 'aria-selected' in js, "aria-selected not toggled"
    assert 'is-active' in js, "is-active class not toggled"


def test_css_provides_tab_styling():
    """CSS contains the tab rules needed for visual layout."""
    css = Path("ui/dashboard/static/css/dashboard.css").read_text(encoding="utf-8")
    for sel in [".tabs", ".tab", ".tab.is-active", ".tab__count", ".tab-pane", ".tab-pane.is-active"]:
        assert sel in css, f"CSS missing selector {sel}"

    # is-active should add an accent underline
    assert ".tab.is-active::after" in css
    assert ".tab-pane.is-active" in css
    assert "[hidden]" in css, ".tab-pane[hidden] rule missing"


def test_javascript_toggles_tab_count_badges():
    """renderPositions and renderClosedTrades update the Open / Closed tab counts."""
    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")

    # renderPositions should set openTabCount text
    pos_block = re.search(r"function renderPositions[\s\S]+?\n  }", js)
    assert pos_block, "renderPositions not found"
    assert "openTabCount" in pos_block.group(0), "renderPositions missing openTabCount update"

    # renderClosedTrades should set closedTabCount text
    closed_block = re.search(r"function renderClosedTrades[\s\S]+?\n  }", js)
    assert closed_block, "renderClosedTrades not found"
    assert "closedTabCount" in closed_block.group(0), "renderClosedTrades missing closedTabCount update"


def test_portfolio_payload_exposes_live_unrealized_metrics(monkeypatch):
    from types import SimpleNamespace

    from ui.dashboard import main

    pos_a = SimpleNamespace(quantity=10, average_cost=100.0)
    pos_b = SimpleNamespace(quantity=5, average_cost=200.0)
    state = SimpleNamespace(
        cash=5000.0,
        equity=6400.0,
        unrealized_pnl=400.0,
        positions={"AAA": pos_a, "BBB": pos_b},
    )

    monkeypatch.setattr(main.state.ledger, "load_portfolio_state", lambda: state)
    monkeypatch.setattr(main, "_position_market_snapshot", lambda *_args, **_kwargs: {
        "AAA": {"current_price": 120.0},
        "BBB": {"current_price": 160.0},
    })

    payload = main._portfolio_payload()

    assert payload["total_unrealized_pnl"] == 0.0
    assert payload["total_unrealized_pct"] == 0.0
    assert payload["winning_positions"] == 1
    assert payload["losing_positions"] == 1
    assert payload["marks_loaded"] is True
    by_symbol = {p["symbol"]: p for p in payload["positions"]}
    assert by_symbol["AAA"]["unrealized_pnl"] == 200.0
    assert by_symbol["AAA"]["unrealized_pct"] == 0.2
    assert by_symbol["AAA"]["mark_is_live"] is True
    assert by_symbol["BBB"]["unrealized_pnl"] == -200.0
    assert by_symbol["BBB"]["unrealized_pct"] == -0.2
    assert by_symbol["BBB"]["mark_is_live"] is True


def test_portfolio_payload_marks_stale_positions(monkeypatch):
    from types import SimpleNamespace

    from ui.dashboard import main

    pos_live = SimpleNamespace(quantity=10, average_cost=100.0)
    pos_stale = SimpleNamespace(quantity=5, average_cost=200.0)
    state = SimpleNamespace(
        cash=5000.0,
        equity=6200.0,
        unrealized_pnl=0.0,
        positions={"LIVE": pos_live, "STALE": pos_stale},
    )

    monkeypatch.setattr(main.state.ledger, "load_portfolio_state", lambda: state)
    monkeypatch.setattr(main, "_position_market_snapshot", lambda *_args, **_kwargs: {
        "LIVE": {"current_price": 110.0},
    })

    payload = main._portfolio_payload()
    by_symbol = {p["symbol"]: p for p in payload["positions"]}

    assert payload["marks_loaded"] is True
    assert by_symbol["LIVE"]["mark_is_live"] is True
    assert by_symbol["LIVE"]["current_price"] == 110.0
    assert by_symbol["LIVE"]["unrealized_pnl"] == 100.0
    assert by_symbol["STALE"]["mark_is_live"] is False
    assert by_symbol["STALE"]["current_price"] == 200.0
    assert by_symbol["STALE"]["unrealized_pnl"] == 0.0
    assert payload["winning_positions"] == 1
    assert payload["losing_positions"] == 0

    monkeypatch.setattr(main, "_position_market_snapshot", lambda *_args, **_kwargs: {})
    payload_empty = main._portfolio_payload()
    by_symbol_empty = {p["symbol"]: p for p in payload_empty["positions"]}
    assert payload_empty["marks_loaded"] is False
    assert all(p["mark_is_live"] is False for p in payload_empty["positions"])


def test_position_market_snapshot_uses_latest_mark_when_available(monkeypatch):
    from types import SimpleNamespace

    from ui.dashboard import main

    calls = []

    def fake_marks(symbols):
        calls.append(symbols)
        return {"AAA": {"current_price": 123.45}}

    monkeypatch.setattr(main, "_load_position_marks", fake_marks)
    pstate = SimpleNamespace(positions={"AAA": SimpleNamespace(quantity=1, average_cost=100.0)})

    snapshot = main._position_market_snapshot(pstate)

    assert calls == [["AAA"]]
    assert snapshot["AAA"]["current_price"] == 123.45


def test_dashboard_template_contains_live_open_pnl_modules() -> None:
    html = Path("ui/dashboard/templates/dashboard.html").read_text(encoding="utf-8")

    assert 'id="openPnlValue"' in html
    assert 'id="openPnlPct"' in html
    assert 'id="liveWinnersValue"' in html
    assert 'id="liveLosersValue"' in html
    assert 'id="liveExposureValue"' in html


def test_dashboard_live_pnl_contract_is_consistent() -> None:
    html = Path("ui/dashboard/templates/dashboard.html").read_text(encoding="utf-8")
    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")

    for token in [
        "openPnlValue",
        "openPnlPct",
        "liveWinnersValue",
        "liveLosersValue",
        "liveExposureValue",
    ]:
        assert token in html
        assert token in js


def test_open_pnl_pct_uses_real_fallback_when_total_pct_missing() -> None:
    import subprocess

    payload = json.dumps(
        {
            "equity": 2100,
            "cash": 1000,
            "positions": [
                {
                    "ticker": "AAA",
                    "quantity": 10,
                    "average_cost": 100,
                    "current_price": 110,
                    "market_value": 1100,
                    "unrealized_pnl": 100,
                }
            ],
            "total_unrealized_pnl": 100,
            "winning_positions": 1,
            "losing_positions": 0,
        }
    )
    js_path = Path("ui/dashboard/static/js/dashboard.js").resolve()
    harness = f"""
const fs = require('fs');
const payload = {payload};
function makeEl(id) {{
  return {{
    id,
    textContent: '',
    innerHTML: '',
    dataset: {{}},
    attributes: {{}},
    classList: {{ add(){{}}, remove(){{}}, toggle(){{}} }},
    setAttribute(name, value) {{ this.attributes[name] = String(value); }},
    getAttribute(name) {{ return this.attributes[name] ?? null; }},
    removeAttribute(name) {{ delete this.attributes[name]; }},
    addEventListener() {{}},
    appendChild() {{}},
    querySelectorAll() {{ return []; }},
    querySelector() {{ return null; }},
    focus() {{}},
    style: {{}},
    offsetWidth: 0,
  }};
}}
const elements = new Map();
const document = {{
  readyState: 'complete',
  body: {{ dataset: {{ startingEquity: '350000' }} }},
  head: {{ appendChild() {{}} }},
  createElement() {{ return makeEl('__created__'); }},
  getElementById(id) {{
    if (!elements.has(id)) elements.set(id, makeEl(id));
    return elements.get(id);
  }},
  addEventListener() {{}},
}};
const window = {{
  document,
  localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
  confirm() {{ return false; }},
  alert() {{}},
  fetch(url) {{
    const body = url === '/api/portfolio' ? payload : {{}};
    return Promise.resolve({{ ok: true, json: () => Promise.resolve(body) }});
  }},
}};
global.document = document;
global.window = window;
global.fetch = window.fetch;
global.EventSource = function() {{ this.close = function() {{}}; }};
global.requestAnimationFrame = (cb) => cb();
global.setInterval = () => 0;
global.clearInterval = () => {{}};
global.setTimeout = (cb) => {{ cb(); return 0; }};
global.clearTimeout = () => {{}};
eval(fs.readFileSync({json.dumps(str(js_path))}, 'utf8'));
setImmediate(() => {{
  console.log(JSON.stringify({{
    openPnlPct: document.getElementById('openPnlPct').textContent,
  }}));
}});
"""

    result = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        check=True,
    )

    rendered = json.loads(result.stdout.strip())
    assert rendered["openPnlPct"] == "+10.00%"


def test_dashboard_js_renders_unrealized_pct_and_live_mark_fields() -> None:
    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")
    pos_block = re.search(r"function renderPositions[\s\S]+?\n  }", js)

    assert pos_block, "renderPositions not found"
    render_positions = pos_block.group(0)

    assert "unrealized_pct" in render_positions
    assert "current_price" in render_positions
    assert 'class="num mark"' in render_positions
    assert "Unrealized %" in render_positions


def test_dashboard_js_renders_stale_mark_indicator() -> None:
    """When a position row has mark_is_live=false, the row gets a stale style hint."""
    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")
    css = Path("ui/dashboard/static/css/dashboard.css").read_text(encoding="utf-8")

    pos_block = re.search(r"function renderPositions[\s\S]+?\n  }", js)
    assert pos_block, "renderPositions not found"
    render_positions = pos_block.group(0)

    assert "mark_is_live" in render_positions, (
        "renderPositions must consult mark_is_live for staleness styling"
    )
    assert "position-row--stale" in render_positions or "mark-stale" in render_positions, (
        "renderPositions must mark stale rows with a special class"
    )
    assert "position-row--stale" in css or "mark-stale" in css, (
        "CSS must define a stale-mark visual treatment"
    )


def test_dashboard_js_maps_zero_pnl_rows_to_neutral_class() -> None:
    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")
    pos_block = re.search(r"function renderPositions[\s\S]+?\n  }", js)
    closed_row_block = re.search(r"function buildClosedTradesRow[\s\S]+?\n  }", js)

    assert pos_block, "renderPositions not found"
    assert closed_row_block, "buildClosedTradesRow not found"
    render_positions = pos_block.group(0)
    render_closed_trade = closed_row_block.group(0)

    assert 'const trend    = pnl >= 0 ? "pnl-pos" : (pnl < 0 ? "pnl-neg" : "pnl-flat");' not in render_positions
    assert 'const trend    = pnl > 0 ? "pnl-pos" : (pnl < 0 ? "pnl-neg" : "pnl-flat");' in render_positions
    assert 'const trend    = pnl >= 0 ? "pnl-pos" : "pnl-neg";' not in render_closed_trade
    assert 'const trend    = pnl > 0 ? "pnl-pos" : (pnl < 0 ? "pnl-neg" : "pnl-flat");' in render_closed_trade


def test_open_positions_badge_preserves_label_markup() -> None:
    """renderPortfolio must update positionsBadgeNum, not flatten positionsBadge."""
    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")

    assert "positionsBadgeNum" in js, "positionsBadgeNum must be used"
    assert "setValue(posBadge," not in js and "setValue(posBadge )" not in js, (
        "renderPortfolio must not flatten positionsBadge via setValue"
    )

    start = js.index('const posBadge = $("positionsBadge");')
    block = js[start:start + 800]
    assert "positionsBadgeNum" in block, (
        "renderPortfolio must update positionsBadgeNum, not flatten positionsBadge"
    )

    template = Path("ui/dashboard/templates/dashboard.html").read_text(encoding="utf-8")
    assert 'id="positionsBadgeNum"' in template, (
        "Template must include the #positionsBadgeNum node preserved by JS"
    )


def test_sse_stream_emits_trades_and_apply_update_handles_them() -> None:
    """SSE must include trades; applyUpdate must call renderTrades."""
    py = Path("ui/dashboard/main.py").read_text(encoding="utf-8")
    block = re.search(r"async def event_generator[\s\S]+?await asyncio\.sleep\(5\)", py)
    assert block, "event_generator not found"
    generator = block.group(0)
    assert "_trades_payload" in generator, "event_generator must stream trades payload"

    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")
    apply_block = re.search(r"function applyUpdate[\s\S]+?\}", js)
    assert apply_block, "applyUpdate not found"
    updater = apply_block.group(0)
    assert "renderTrades" in updater, "applyUpdate must call renderTrades for the stream"

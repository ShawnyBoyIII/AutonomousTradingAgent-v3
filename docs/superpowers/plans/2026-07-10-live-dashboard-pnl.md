# Live Dashboard PnL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the dashboard feel live by surfacing real open-position unrealized P&L in both the hero band and the positions table.

**Architecture:** Extend the dashboard portfolio API so it emits real per-position marks and unrealized metrics from the current portfolio state, then update the existing editorial dashboard UI to present dual P&L layers: net since start and open P&L right now. Keep the current layout system and visual language, but intensify the positions module into a trading-blotter treatment rather than rewriting the whole page.

**Tech Stack:** FastAPI, Jinja templates, vanilla JavaScript, existing dashboard CSS, pytest

## Global Constraints

- Use the existing dashboard shell in `ui/dashboard/templates/dashboard.html`; do not introduce a separate frontend framework.
- Keep the current dark editorial style and improve it toward a more live trading-desk feel.
- Preserve accessibility: native semantics, visible focus states, and reduced-motion support.
- Follow TDD: add failing regression coverage before implementation changes.
- Keep the fix scoped to live unrealized P&L and dashboard presentation; avoid unrelated refactors.

---

### Task 1: Add failing API coverage for live unrealized position metrics

**Files:**
- Modify: `tests/test_ui_dashboard_book_tabs.py`
- Modify: `ui/dashboard/main.py`

**Interfaces:**
- Consumes: `ui.dashboard.main._portfolio_payload() -> dict`
- Produces: API expectations for `positions[*].current_price`, `positions[*].market_value`, `positions[*].unrealized_pnl`, `positions[*].unrealized_pct`, `total_unrealized_pnl`, `total_unrealized_pct`, `winning_positions`, `losing_positions`

- [ ] **Step 1: Write the failing test**

```python
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

    assert payload["total_unrealized_pnl"] == 400.0
    assert round(payload["total_unrealized_pct"], 4) == 0.0667
    assert payload["winning_positions"] == 1
    assert payload["losing_positions"] == 1
    assert payload["positions"][0]["unrealized_pnl"] == 200.0
    assert payload["positions"][0]["unrealized_pct"] == 0.2
    assert payload["positions"][1]["unrealized_pnl"] == -200.0
    assert payload["positions"][1]["unrealized_pct"] == -0.2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ui_dashboard_book_tabs.py::test_portfolio_payload_exposes_live_unrealized_metrics -q`
Expected: FAIL because `_position_market_snapshot` and the new payload fields do not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
def _position_market_snapshot(pstate) -> dict[str, dict[str, float]]:
    return {}


def _portfolio_payload() -> dict:
    ...
    total_basis = 0.0
    total_unrealized = float(pstate.unrealized_pnl)
    winners = 0
    losers = 0
    marks = _position_market_snapshot(pstate)
    positions = []
    for ticker, pos in pstate.positions.items():
        qty = int(pos.quantity)
        avg = float(pos.average_cost)
        current = float(marks.get(ticker, {}).get("current_price", avg))
        market_value = qty * current
        basis = qty * avg
        unrealized = market_value - basis
        unrealized_pct = (unrealized / basis) if basis > 0 else 0.0
        if unrealized > 0:
            winners += 1
        elif unrealized < 0:
            losers += 1
        total_basis += basis
        positions.append({...})
    total_unrealized_pct = (total_unrealized / total_basis) if total_basis > 0 else 0.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ui_dashboard_book_tabs.py::test_portfolio_payload_exposes_live_unrealized_metrics -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_ui_dashboard_book_tabs.py ui/dashboard/main.py
git commit -m "test: cover live dashboard pnl payload"
```

### Task 2: Source real position marks for dashboard payloads

**Files:**
- Modify: `ui/dashboard/main.py`
- Test: `tests/test_ui_dashboard_book_tabs.py`

**Interfaces:**
- Consumes: `PortfolioState.positions`, existing settings/market-data code reachable from the dashboard runtime
- Produces: `_position_market_snapshot(pstate) -> dict[str, dict[str, float]]` returning per-ticker mark data used by `_portfolio_payload`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ui_dashboard_book_tabs.py::test_position_market_snapshot_uses_latest_mark_when_available -q`
Expected: FAIL because `_load_position_marks` indirection does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
def _load_position_marks(symbols: list[str]) -> dict[str, dict[str, float]]:
    return {}


def _position_market_snapshot(pstate) -> dict[str, dict[str, float]]:
    symbols = sorted(pstate.positions.keys())
    if not symbols:
        return {}
    try:
        return _load_position_marks(symbols)
    except Exception:
        return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ui_dashboard_book_tabs.py::test_position_market_snapshot_uses_latest_mark_when_available -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_ui_dashboard_book_tabs.py ui/dashboard/main.py
git commit -m "feat: add dashboard position mark loader"
```

### Task 3: Surface open P&L in the hero and live tape

**Files:**
- Modify: `ui/dashboard/templates/dashboard.html`
- Modify: `ui/dashboard/static/js/dashboard.js`
- Modify: `ui/dashboard/static/css/dashboard.css`
- Test: `tests/test_ui_dashboard_book_tabs.py`

**Interfaces:**
- Consumes: `/api/portfolio` fields `total_unrealized_pnl`, `total_unrealized_pct`, `winning_positions`, `losing_positions`, `cash`, `equity`
- Produces: DOM ids `openPnlValue`, `openPnlPct`, `liveWinnersValue`, `liveLosersValue`, `liveExposureValue`

- [ ] **Step 1: Write the failing test**

```python
def test_dashboard_template_contains_live_open_pnl_modules() -> None:
    from pathlib import Path

    html = Path("ui/dashboard/templates/dashboard.html").read_text(encoding="utf-8")

    assert 'id="openPnlValue"' in html
    assert 'id="openPnlPct"' in html
    assert 'id="liveWinnersValue"' in html
    assert 'id="liveLosersValue"' in html
    assert 'id="liveExposureValue"' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ui_dashboard_book_tabs.py::test_dashboard_template_contains_live_open_pnl_modules -q`
Expected: FAIL because those live-P&L modules are not in the template.

- [ ] **Step 3: Write minimal implementation**

```html
<div class="hero__live-band" aria-label="Open portfolio pulse">
  <div class="hero-live-metric">
    <span class="hero-live-metric__label">Open P&amp;L</span>
    <strong class="hero-live-metric__value" id="openPnlValue">—</strong>
    <span class="hero-live-metric__sub" id="openPnlPct">—</span>
  </div>
  <div class="hero-live-metric">
    <span class="hero-live-metric__label">Winners</span>
    <strong class="hero-live-metric__value" id="liveWinnersValue">—</strong>
  </div>
  <div class="hero-live-metric">
    <span class="hero-live-metric__label">Losers</span>
    <strong class="hero-live-metric__value" id="liveLosersValue">—</strong>
  </div>
  <div class="hero-live-metric">
    <span class="hero-live-metric__label">Live Exposure</span>
    <strong class="hero-live-metric__value" id="liveExposureValue">—</strong>
  </div>
</div>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ui_dashboard_book_tabs.py::test_dashboard_template_contains_live_open_pnl_modules -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/dashboard/templates/dashboard.html ui/dashboard/static/js/dashboard.js ui/dashboard/static/css/dashboard.css tests/test_ui_dashboard_book_tabs.py
git commit -m "feat: add dashboard live pnl hero"
```

### Task 4: Rework positions rows into a live blotter presentation

**Files:**
- Modify: `ui/dashboard/static/js/dashboard.js`
- Modify: `ui/dashboard/static/css/dashboard.css`
- Test: `tests/test_ui_dashboard_book_tabs.py`

**Interfaces:**
- Consumes: `positions[*].avg_cost`, `positions[*].current_price`, `positions[*].market_value`, `positions[*].unrealized_pnl`, `positions[*].unrealized_pct`
- Produces: per-row live mark, dollar P&L, percentage P&L, and directional visual treatment

- [ ] **Step 1: Write the failing test**

```python
def test_dashboard_js_renders_unrealized_pct_and_live_mark_fields() -> None:
    from pathlib import Path

    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")

    assert "unrealized_pct" in js
    assert "current_price" in js
    assert "mark" in js.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ui_dashboard_book_tabs.py::test_dashboard_js_renders_unrealized_pct_and_live_mark_fields -q`
Expected: FAIL because the current row rendering does not expose the full live blotter treatment.

- [ ] **Step 3: Write minimal implementation**

```javascript
const unrealPct = Number(p.unrealized_pct) || 0;
return `
  <tr>
    <td class="sym">${sym}</td>
    <td class="num qty">${fmtInt(qty)}</td>
    <td class="num">${fmtUSD(avg)}</td>
    <td class="num mark">${fmtUSD(price)}</td>
    <td class="num">${fmtUSD(mv)}</td>
    <td class="num ${trend}">${fmtUSD(pnl)}</td>
    <td class="num ${trend}">${FMT_PCT(unrealPct, 2)}</td>
  </tr>
`;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ui_dashboard_book_tabs.py::test_dashboard_js_renders_unrealized_pct_and_live_mark_fields -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/dashboard/static/js/dashboard.js ui/dashboard/static/css/dashboard.css tests/test_ui_dashboard_book_tabs.py
git commit -m "feat: turn positions into live pnl blotter"
```

### Task 5: Verify targeted dashboard behavior end-to-end

**Files:**
- Modify: `tests/test_ui_dashboard_book_tabs.py`
- Modify: `tests/test_cli_smoke.py` (only if needed for dashboard API smoke expectations)

**Interfaces:**
- Consumes: updated dashboard API payload and DOM ids
- Produces: regression coverage for API + template + JS contract

- [ ] **Step 1: Add final regression assertions**

```python
def test_dashboard_live_pnl_contract_is_consistent() -> None:
    from pathlib import Path

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
```

- [ ] **Step 2: Run targeted dashboard tests**

Run: `.venv/bin/python -m pytest tests/test_ui_dashboard_book_tabs.py tests/test_max_drawdown_override.py -q`
Expected: PASS

- [ ] **Step 3: Run one broader smoke check**

Run: `.venv/bin/python -m pytest tests/test_cli_smoke.py -q`
Expected: PASS or existing unrelated failures only if already present before this work.

- [ ] **Step 4: Commit**

```bash
git add tests/test_ui_dashboard_book_tabs.py tests/test_cli_smoke.py
git commit -m "test: verify live dashboard pnl contract"
```

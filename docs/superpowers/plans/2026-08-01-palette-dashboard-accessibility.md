# Palette Dashboard Accessibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete PR #35 with production dashboard accessibility behavior and regression coverage for timestamps, tab panels, and expanded closed trades.

**Architecture:** Keep the existing dependency-free IIFE in `dashboard.js`. Add one boolean to its existing `STATE` object, use it when rendering closed trades, and keep focus changes limited to explicit expansion or a previously focused element inside the closed-trade region. Extend the existing source-contract dashboard tests rather than introducing a browser test dependency.

**Tech Stack:** Vanilla ES2017 JavaScript, Jinja HTML templates, pytest, Node-based source harnesses already used by dashboard tests.

## Global Constraints

- All tests remain deterministic and network-free.
- Python >= 3.11 is required.
- Do not add frontend dependencies or change the dashboard polling/SSE architecture.
- New regression coverage belongs under `tests/`; the root `test_focus.html` fixture is removed.
- Preserve existing ARIA tab selection, `hidden` state, and five-second update behavior.

---

### Task 1: Add Failing Accessibility Regression Tests

**Files:**
- Modify: `tests/test_ui_dashboard_book_tabs.py`
- Modify: `tests/test_ui_dashboard_recent_trades.py`
- Modify: `tests/test_ui_dashboard_closed_trades.py`
- Delete: `test_focus.html`

**Interfaces:**
- Tests consume the current dashboard template and `ui/dashboard/static/js/dashboard.js` source contracts.
- Tests produce failures that identify missing `tabindex`, accessible timestamp labels, expansion state, and focus-preservation logic before implementation.

- [ ] **Step 1: Add the template accessibility test**

Add this test to `tests/test_ui_dashboard_book_tabs.py`:

```python
def test_tabpanels_are_keyboard_focus_targets():
    """Every dashboard tabpanel can receive focus when activated."""
    html = Path("ui/dashboard/templates/dashboard.html").read_text(encoding="utf-8")
    for pane_id in ("openPane", "closedPane", "todayPane", "tradeCohortPane", "equityCohortPane"):
        pane_match = re.search(rf'<div[^>]*id="{pane_id}"[^>]*>', html)
        assert pane_match, f"Missing tabpanel {pane_id}"
        assert 'role="tabpanel"' in pane_match.group(0)
        assert 'tabindex="0"' in pane_match.group(0), f"{pane_id} is not focusable"
```

- [ ] **Step 2: Add the timestamp and expansion source-contract tests**

Add these tests to the existing dashboard test modules:

```python
def test_timestamp_tooltips_are_keyboard_accessible():
    """Exact timestamps are reachable and named for keyboard users."""
    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")
    assert 'class="alert__time"' in js
    assert 'class="trade__time"' in js
    assert 'tabindex="0"' in js
    assert 'aria-label="Exact time:' in js


def test_closed_trade_expansion_preserves_state_and_focus():
    """Expansion is explicit, focusable, and not reset by background updates."""
    js = Path("ui/dashboard/static/js/dashboard.js").read_text(encoding="utf-8")
    assert "closedTradesExpanded" in js
    assert "focusExpanded" in js
    assert "document.activeElement" in js
    assert '.focus()' in js
    assert 'tabindex="-1"' in js
```

Place the timestamp test in `tests/test_ui_dashboard_recent_trades.py` and the
closed-trade test in `tests/test_ui_dashboard_closed_trades.py`.

- [ ] **Step 3: Remove the standalone demo fixture**

Delete `test_focus.html`; it is outside pytest discovery and does not load
production dashboard code.

- [ ] **Step 4: Run the focused tests and verify the expected failures**

Run:

```bash
.venv/bin/python -m pytest tests/test_ui_dashboard_book_tabs.py tests/test_ui_dashboard_recent_trades.py tests/test_ui_dashboard_closed_trades.py -q
```

Expected: failures for the new assertions because the current PR head has no
production accessibility implementation.

- [ ] **Step 5: Commit the failing tests and fixture removal**

```bash
git add tests/test_ui_dashboard_book_tabs.py tests/test_ui_dashboard_recent_trades.py tests/test_ui_dashboard_closed_trades.py test_focus.html
git commit -m "test: cover palette dashboard accessibility"
```

### Task 2: Implement Production Accessibility and Focus State

**Files:**
- Modify: `ui/dashboard/static/js/dashboard.js:11-22, 480-553, 619-630, 763-795`
- Modify: `ui/dashboard/templates/dashboard.html:229-335`

**Interfaces:**
- `renderClosedTrades(data, options = {})` accepts `options.focusExpanded`, which is `false` for polling and `true` only for the user click path.
- `STATE.closedTradesExpanded` persists expansion across `renderClosedTrades` calls during SSE updates.

- [ ] **Step 1: Add closed-trade state and render options**

Extend `STATE` with:

```javascript
closedTradesExpanded: false,
```

Change the renderer signature to `renderClosedTrades(data, options = {})` and
capture the focus state before replacing markup:

```javascript
const focusExpanded = options.focusExpanded === true;
const focusWasInside = container.contains(document.activeElement);
```

Reset `STATE.closedTradesExpanded` when there are no trades. Choose all rows
when the state is expanded and only the first six rows otherwise:

```javascript
const visible = STATE.closedTradesExpanded
  ? trades
  : trades.slice(0, INITIAL_LIMIT);
```

- [ ] **Step 2: Make the expanded table a safe focus target**

Render the table with `tabindex="-1"` and keep the expand button only when the
list is collapsed. After rendering, focus the replacement table only when the
user explicitly expanded the list or focus was already inside the closed-trade
region:

```javascript
const table = container.querySelector(".closed-table");
if (table && (focusExpanded || focusWasInside)) table.focus();
```

Replace the current click handler body with:

```javascript
expandBtn.addEventListener("click", () => {
  STATE.closedTradesExpanded = true;
  renderClosedTrades({ trades }, { focusExpanded: true });
});
```

This keeps background updates from stealing focus from unrelated controls while
preserving context when a focused control inside the closed-trade region is
re-rendered.

- [ ] **Step 3: Make exact timestamps keyboard accessible**

Add `tabindex="0"` and an escaped exact-time accessible name to the alert
timestamp, last-fill timestamp, and recent-trade timestamp spans. Retain the
existing `title` attribute and relative display text:

```javascript
<span class="trade__time"
      tabindex="0"
      aria-label="Exact time: ${escapeHTML(FMT_EXACT(t.timestamp))}"
      title="${escapeHTML(FMT_EXACT(t.timestamp))}">
```

Use the same structure for the alert and telemetry timestamp spans.

- [ ] **Step 4: Make tab panels keyboard focus targets**

Add `tabindex="0"` to the five existing `role="tabpanel"` elements in
`dashboard.html`. Do not change their IDs, `aria-labelledby`, `hidden`, or
active-class behavior.

- [ ] **Step 5: Run focused tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_ui_dashboard_book_tabs.py tests/test_ui_dashboard_recent_trades.py tests/test_ui_dashboard_closed_trades.py -q
```

Expected: all focused dashboard tests pass.

- [ ] **Step 6: Commit the production implementation**

```bash
git add ui/dashboard/static/js/dashboard.js ui/dashboard/templates/dashboard.html
git commit -m "fix: complete palette dashboard accessibility"
```

### Task 3: Full Verification and PR Update

**Files:**
- Verify: `docs/superpowers/specs/2026-08-01-palette-dashboard-accessibility-design.md`
- Verify: `docs/superpowers/plans/2026-08-01-palette-dashboard-accessibility.md`

**Interfaces:**
- The branch must contain the design commit, failing-test-to-fix commits, and no untracked files.
- The existing PR head must be updated on `origin` without changing `main`.

- [ ] **Step 1: Run the full network-free test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: the complete suite passes with no network calls.

- [ ] **Step 2: Inspect the final diff and worktree**

Run:

```bash
git status --short --branch
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

Expected: only the design/plan docs, dashboard JS/template, and dashboard
tests are changed; `test_focus.html` is absent; there are no whitespace errors
or untracked files.

- [ ] **Step 3: Push the corrected PR branch**

```bash
git push origin HEAD:palette-ux-improvements-18333645516904917850
```

- [ ] **Step 4: Verify GitHub CI and PR state**

Run:

```bash
gh pr view 35 --repo ShawnyBoyIII/AutonomousTradingAgent-v3 --json state,mergeable,mergeStateStatus,statusCheckRollup,url
```

Expected: PR #35 remains open, is mergeable, and its required `test` check is
successful before any merge decision.

- [ ] **Step 5: Commit or push only after verification**

No additional commit is needed after the verified implementation commits;
the final branch must be clean locally, and the remote PR branch must point to
the same tested commit.

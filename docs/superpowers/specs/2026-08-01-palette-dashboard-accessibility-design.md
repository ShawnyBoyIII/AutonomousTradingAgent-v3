# Palette Dashboard Accessibility Design

## Goal

Complete PR #35 so the live dashboard, rather than a standalone demo file,
supports keyboard access to exact timestamps and preserves keyboard context
when closed trades are expanded or refreshed.

## Scope

- Make exact-time tooltip elements keyboard focusable and expose the exact
  timestamp as an accessible label.
- Make dashboard tab panels valid keyboard focus targets.
- Preserve the user's expanded closed-trade view across five-second SSE
  updates.
- Move focus to the expanded closed-trades table after an explicit
  "Show more" activation, without stealing focus during background updates.
- Add regression coverage under `tests/` and remove the standalone
  `test_focus.html` fixture.

## Design

`dashboard.js` will use the existing dashboard state object to track whether
the closed-trades list is expanded. `renderClosedTrades` will render all
available rows while that state is active. The click path will explicitly
focus the rebuilt table, while ordinary polling will only preserve focus when
the user was already inside the closed-trades region.

Timestamp spans will retain their native `title` display and gain
`tabindex="0"` plus an escaped accessible exact-time label. Existing tabpanel
elements will gain `tabindex="0"` without changing tab selection or hidden
state behavior.

## Testing

- Extend the existing dashboard tests to assert timestamp focusability and
  tabpanel focus targets from the rendered/source contract.
- Add a focused closed-trades regression covering explicit expansion,
  `document.activeElement`, and preservation after a background update.
- Confirm the existing dashboard suite and full network-free suite remain
  green.
- Ensure the root-level manual HTML fixture is removed and is not part of the
  test contract.

## Acceptance Criteria

- The PR diff changes the production dashboard JS/template and tests.
- Keyboard users can reach exact timestamps with Tab.
- Activating "Show more" reveals all rows and moves focus to the table.
- An SSE update does not collapse an already expanded list or steal focus from
  an unrelated control.
- No network calls are introduced and all existing tests pass.

## Non-Goals

- Replacing the dashboard's polling or SSE architecture.
- Introducing a frontend framework or a new accessibility dependency.
- Redesigning the dashboard visual layout.

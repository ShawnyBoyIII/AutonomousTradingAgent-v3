## 2024-06-27 - Loading states and accessibility for async UI actions
**Learning:** Found that the destructive/async "Kill Switch" button lacked visual and accessible feedback while processing its request, which could lead to multiple clicks or confusion. Implementing native loading text and `aria-busy="true"` on the button improves the UX pattern here without needing custom spinner icons.
**Action:** When adding or auditing buttons for async endpoints, always ensure `.disabled` logic is paired with UX text changes (like "Halting...") and `aria-busy` for screen readers. Keep `.btn:focus-visible` styles robust for keyboard users.
## 2026-07-01 - Semantic Landmarks and Screen Reader Table Navigation
**Learning:** The dashboard previously lacked semantic HTML landmarks (like `<header>` and `<main>`) and used basic `<th>` tags for tables, which made it harder for assistive technologies to map page layout and data relationships.
**Action:** Implemented semantic landmark wrapping in dashboard templates and added `scope="col"` to table headers for consistent screen reader accessibility.
## 2024-07-28 - Async Loading State for Kill Switch
**Learning:** Found that the "Kill Switch" button on the dashboard lacked an accessible async loading state. When pulled, it did not provide visual feedback to users that the action was in progress, nor did it announce its busy state to screen readers.
**Action:** Implemented `aria-busy="true"` and changed the visual label to "Halting..." on click. Reverted these states properly both on successful update and error catch block.
## 2024-05-18 - Tabindex and Title Accessibility in Dashboard
**Learning:** Found that the "skip to main content" link pointed to `<main id="main">` which lacked a `tabindex="-1"`, meaning keyboard focus was not properly managed by the browser upon skip link activation. Also found that the "Kill Switch" lever lacked descriptive tooltips (`title` attribute) communicating its disabled or ready states.
**Action:** When creating skip links, always ensure the target container (e.g. `<main>`) has `tabindex="-1"`. When a button's disabled state changes, dynamically update its `title` attribute so hover and screen readers provide actionable feedback on *why* it is disabled.
## 2024-05-24 - Timestamp Clarity
**Learning:** In trading dashboards, relative time displays (e.g., "5s ago") are great for quick parsing, but users frequently need exact execution times to cross-reference with market data.
**Action:** Always pair relative times with absolute timestamp tooltips (`title` attributes) so exact context is available on hover without cluttering the UI.

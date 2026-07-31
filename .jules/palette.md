## 2024-06-27 - Loading states and accessibility for async UI actions
**Learning:** Found that the destructive/async "Kill Switch" button lacked visual and accessible feedback while processing its request, which could lead to multiple clicks or confusion. Implementing native loading text and `aria-busy="true"` on the button improves the UX pattern here without needing custom spinner icons.
**Action:** When adding or auditing buttons for async endpoints, always ensure `.disabled` logic is paired with UX text changes (like "Halting...") and `aria-busy` for screen readers. Keep `.btn:focus-visible` styles robust for keyboard users.
## 2026-07-01 - Semantic Landmarks and Screen Reader Table Navigation
**Learning:** The dashboard previously lacked semantic HTML landmarks (like `<header>` and `<main>`) and used basic `<th>` tags for tables, which made it harder for assistive technologies to map page layout and data relationships.
**Action:** Implemented semantic landmark wrapping in dashboard templates and added `scope="col"` to table headers for consistent screen reader accessibility.
## 2024-07-28 - Async Loading State for Kill Switch
**Learning:** Found that the "Kill Switch" button on the dashboard lacked an accessible async loading state. When pulled, it did not provide visual feedback to users that the action was in progress, nor did it announce its busy state to screen readers.
**Action:** Implemented `aria-busy="true"` and changed the visual label to "Halting..." on click. Reverted these states properly both on successful update and error catch block.
## 2024-07-28 - Dynamic Page Titles for Background Monitoring
**Learning:** For a dashboard like this, users often have it open in a background tab while doing other work. They shouldn't have to switch tabs just to see if the system is halted or to check their P&L.
**Action:** When working on dashboards with critical live states (like P&L or system health), dynamically update the `<title>` tag with a high-level summary (e.g. "🚨 HALTED" or "+$1.2K"). This provides ambient awareness and reduces context switching.

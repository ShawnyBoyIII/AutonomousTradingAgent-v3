## 2024-06-27 - Loading states and accessibility for async UI actions
**Learning:** Found that the destructive/async "Kill Switch" button lacked visual and accessible feedback while processing its request, which could lead to multiple clicks or confusion. Implementing native loading text and `aria-busy="true"` on the button improves the UX pattern here without needing custom spinner icons.
**Action:** When adding or auditing buttons for async endpoints, always ensure `.disabled` logic is paired with UX text changes (like "Halting...") and `aria-busy` for screen readers. Keep `.btn:focus-visible` styles robust for keyboard users.
## 2026-07-01 - Semantic Landmarks and Screen Reader Table Navigation
**Learning:** The dashboard previously lacked semantic HTML landmarks (like `<header>` and `<main>`) and used basic `<th>` tags for tables, which made it harder for assistive technologies to map page layout and data relationships.
**Action:** Implemented semantic landmark wrapping in dashboard templates and added `scope="col"` to table headers for consistent screen reader accessibility.

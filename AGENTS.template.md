# AGENTS.md - <Project Name>

Critical context for Codex sessions.

Keep this file short, concrete, and operational. If a rule is not likely to change the agent's behavior, delete it.

---

## Entry Point

**Always use this command path first:**
```bash
<preferred local command>
<preferred local command with common flags>
```

Never use:
- `<avoid stale global command>`
- `<avoid direct prod/live command>`

If setup matters, say it here:
```bash
<venv activation / package manager / dev server / bootstrap command>
```

---

## Hard Rules

1. **Do not break production data** - describe the safety boundary in one line.
2. **Do not edit tests to make bugs pass** unless explicitly asked.
3. **No real network or paid API calls in tests** - stub or monkeypatch them.
4. **Use existing project patterns first** - prefer reuse over new abstractions.
5. **Do not commit secrets** - `.env`, env vars, secret manager only.
6. **Keep changes scoped** - avoid unrelated refactors while fixing a task.
7. **Run the smallest real verification** before claiming done.

Replace any rule above with project-specific rules that actually matter.

---

## Project Map

```text
<root>/
├── <app or package>/        # Main app
├── <src or api>/            # Business logic / endpoints
├── <tests>/                 # Test suite
├── <scripts>/               # Dev or ops scripts
└── <docs>/                  # Specs / ADRs / runbooks
```

**Important paths:**
- `<path/to/main/entrypoint>` - what starts the app
- `<path/to/core/module>` - main domain logic
- `<path/to/config>` - runtime config
- `<path/to/tests>` - fastest confidence check

---

## Architecture

**Stack:**
- `<language + version>`
- `<framework>`
- `<database / queue / hosting>`

**Flow:**
1. `<input enters system>`
2. `<core transformation or decision point>`
3. `<write / response / side effect>`

**Design notes:**
- `<one important invariant>`
- `<one important dependency boundary>`
- `<one common source of bugs>`

---

## Common Commands

```bash
# setup
<install command>
<bootstrap command>

# run
<dev run command>
<app smoke command>

# tests
<fast test command>
<full test command>

# lint / typecheck / build
<lint command>
<typecheck command>
<build command>
```

If one command should be preferred by default, say so explicitly.

---

## Coding Rules

- Prefer editing existing code over adding new layers.
- Prefer simple functions over classes unless the repo already leans class-heavy.
- Match the repo's naming, file layout, and error-handling style.
- Leave short comments only where intent would otherwise be hard to infer.
- If a shared function is the root cause, fix it there instead of patching each caller.

Add only project-specific rules below:
- `<example: server actions only in app/actions>`
- `<example: never import browser code into node-only modules>`
- `<example: keep SQL in repository layer>`

---

## Testing

**Default verification path:**
```bash
<single best confidence command>
```

**Targeted checks:**
```bash
<single test file command>
<single focused smoke test>
```

**Testing requirements:**
- `<deterministic fixtures / tmp_path / mocks>`
- `<no external services>`
- `<special env vars or seed data if needed>`

---

## Config And Secrets

**Config lives in:**
- `<path>`

**Rules:**
- `<how paths resolve>`
- `<where secrets come from>`
- `<what must never be changed automatically>`

---

## Known Gotchas

- `<example: paths are relative to config file, not repo root>`
- `<example: auth expires after 1 hour>`
- `<example: local build output must exist before app boots>`
- `<example: one package is intentionally pinned>`

Only keep the surprises that actually save time.

---

## Key Files

- `<path/to/file>` - `<why it matters>`
- `<path/to/file>` - `<why it matters>`
- `<path/to/file>` - `<why it matters>`
- `<path/to/doc>` - `<where deeper context lives>`

---

## Current State

- `<what is stable>`
- `<what is in progress>`
- `<what is intentionally disabled or feature-flagged>`

---

## Good AGENTS.md Rules Of Thumb

- Write for future sessions, not humans browsing docs.
- Use exact commands, paths, and boundaries.
- Prefer 5 sharp rules over 30 vague ones.
- Delete stale status notes quickly.
- If the file gets long, split deep detail into docs and link them from `Key Files`.

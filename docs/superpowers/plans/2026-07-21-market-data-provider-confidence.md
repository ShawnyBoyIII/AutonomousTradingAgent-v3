# Market Data Provider Confidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the existing provider fallback stack one normalized source of truth for capabilities, readiness, and ordering.

**Architecture:** Add an immutable registry beside the provider protocol. Reuse it from market-data fallback and the network-free doctor output; keep provider construction, cache keys, and fetch APIs unchanged.

**Tech Stack:** Python 3.11+, dataclasses, pytest.

## Global Constraints

- Add no provider or dependency.
- Never perform network calls from readiness checks or `doctor`.
- Never expose credential values.
- Preserve configured daily ordering and existing intraday priority.

---

### Task 1: Provider profiles and readiness

**Files:**
- Create: `trading_bot/data/providers/registry.py`
- Create: `tests/test_provider_registry.py`

**Interfaces:**
- Produces: `ProviderCapabilities`, `ProviderReadiness`, `get_provider_capabilities`, `provider_readiness`, and `order_provider_names`.

- [x] Write tests for known capabilities, missing/present credentials, unknown providers, and daily/intraday ordering.
- [x] Run the focused tests and confirm they fail because the registry is absent.
- [x] Implement the immutable registry using only the standard library.
- [x] Re-run the focused tests and confirm they pass.

### Task 2: Fetch and doctor integration

**Files:**
- Modify: `trading_bot/data/market_data.py`
- Modify: `trading_bot/cli/app.py`
- Modify: `tests/test_market_data_providers.py`
- Modify: `tests/test_cli_smoke.py`

**Interfaces:**
- Consumes: registry ordering, interval support, and readiness.
- Produces: capability-aware fallback and consistent credential diagnostics.

- [x] Add failing tests for skipping an unsupported interval and reporting missing Finnhub credentials.
- [x] Route provider ordering and preflight capability checks through the registry.
- [x] Route doctor credential status through `provider_readiness`.
- [x] Run focused provider, cache, config, and CLI tests (`66 passed`).

### Task 3: Operational contract and verification

**Files:**
- Modify: `AGENTS.md`

**Interfaces:**
- Produces: current provider fallback and readiness documentation.

- [x] Document the registry as the source of truth for provider capabilities and auth readiness.
- [x] Run `.venv/bin/python -m pytest -q` (`2030 passed`).
- [x] Run `git diff --check` and inspect the scoped diff.

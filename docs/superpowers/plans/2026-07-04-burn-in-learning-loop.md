# EOD Data Pipeline + Nightly Learning Loop — Session Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a nightly cron that downloads massive.com S3 flat-files (day-aggregates, minute-aggregates) into a partitioned Parquet store, then feeds a realised-vol heuristic into `tuning_overrides.py` and a data-store coverage log into `daily_supermodel.py`. Quotes/trades deferred — the test API key does not have read permission for those products (verified via real 403 from the S3 gateway).

**Architecture:** In-tree, no new deps. Two modules — `trading_bot/data/data_store.py` (Parquet + SQLite manifest) and `trading_bot/data/eod_fetcher.py` (AWS SigV4 over `requests` + gzip auto-decompress) — plus a thin `eod_runner.py` orchestrator. Conf via `EodDataStoreSettings` Pydantic block. Cron wired into `scripts/auto-burn-in.sh::run_eod_data_download()` between daily discovery and nightly tune.

**Tech Stack:** Python 3.11+, `requests` (stdlib urllib3), `pyarrow`+`pandas` for Parquet, `sqlite3`, AWS SigV4 reference (RFC 6234).

## Global Constraints

- Python ≥ 3.11 (per `pyproject.toml`)
- NumPy < 2 (pinned in `pyproject.toml`)
- live_trading_enabled forced false in `trading_bot/config/loader.py`
- No new third-party deps — use stdlib + existing project deps only
- TLS verify MUST be controllable via config (`eod_data_store.verify_tls`, `eod_data_store.tls_ca_bundle`)
- Addressing-style MUST be controllable via config (`eod_data_store.addressing_style`)
- Auth-mode MUST be controllable via config (`eod_data_store.auth_mode` = `sigv4` | `bearer`)
- Credentials via env vars only (`MASSIVE_S3_ACCESS_KEY_ID`, `MASSIVE_S3_SECRET_ACCESS_KEY`, `MASSIVE_S3_BEARER_TOKEN`, `MASSIVE_S3_REGION`); loader rejects hardcoded api_key/secret strings in YAML
- SigV4 MUST include `x-amz-checksum-mode: ENABLED` in canonical request (massive.com gateway requirement)
- Defaults are conservative — `intervals: [1d, 1m]`, `verify_tls: false`, `addressing_style: path` (verified live)
- Tests MUST be network-free (monkeypatch `_request` or use `monkeypatch`)
- Idempotent nightly run — single `.last_eod_fetch_YYYY-MM-DD.marker` written only when at least one interval succeeds
- Per-interval failure MUST NOT abort the other intervals (per-product try/except in runner)
- Path layout: `us_stocks_sip/<dataset>/{year}/{month}/{date}.csv.gz` (verified live)
- 4 products supported in code+config but quotes/trades 403 on test key — defaults stay `[1d, 1m]`

---

## File Structure

| File | Responsibility |
|---|---|
| `trading_bot/data/eod_fetcher.py` | AWS SigV4 signing, gzip auto-decompress, retry, addressing-style, verify_tls, tls_ca_bundle, auth_mode (sigv4/bearer) |
| `trading_bot/data/data_store.py` | Parquet write (partitioned by `parquet/{symbol}/{interval}/{year}/{month}/{date}.parquet`), SQLite manifest, atomic marker writer |
| `trading_bot/data/eod_runner.py` | CLI dispatch orchestration — date → intervals → products → fetch → persist → marker |
| `trading_bot/config/settings.py` | Add `EodDataStoreSettings` Pydantic block |
| `trading_bot/cli/app.py` | Add `eod-fetch` typer command |
| `scripts/auto-burn-in.sh` | Add `run_eod_data_download()` shell function with `EOD_DATA_STORE` env gate |
| `trading_bot/learning/tuning_overrides.py` | Add `_maybe_nudge_window_from_data_store()` realised-vol heuristic |
| `scripts/daily_supermodel.py` | Add data-store coverage logging |
| `burn-in-config.yaml`, `config.yaml` | Add `eod_data_store:` block (intervals, key templates, verify, addressing) |
| `.env.example` | Add the 4 `MASSIVE_S3_*` vars |
| `tests/test_eod_fetcher.py`, `tests/test_data_store.py`, `tests/test_eod_runner.py`, `tests/test_auto_burn_in_script.py` | Regression tests |

---

### Task 1: AWS SigV4 reference vectors (security-critical)

**Files:**
- Create: `tests/test_sigv4_reference.py`

**Interfaces:**
- Consumes: `trading_bot.data.eod_fetcher._signing_key`, `trading_bot.data.eod_fetcher._signature`
- Produces: confirms we match AWS's published reference signature `5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7`

- [ ] **Step 1: Write the failing test** — `docs/superpowers/plans/2026-07-04-burn-in-learning-loop.md` already documents the expected reference vectors; copy them verbatim.
- [ ] **Step 2: Run test, confirm FAIL** with "ImportError: cannot import _signing_key".
- [ ] **Step 3: Implement `_signing_key(secret, date_stamp, region, service)` and `_signature(key, msg)` helpers** per RFC 6234.
- [ ] **Step 4: Run test, confirm PASS** with all 4 reference vectors.
- [ ] **Step 5: Commit** `git commit -m "test(eod): add AWS SigV4 reference vector"` (use only if user has asked for commit — they have not for this feature; skip).

### Task 2: S3 GET with signing + addressing-style + verify_tls + auth_mode

**Files:**
- Create: `trading_bot/data/eod_fetcher.py`
- Test: `tests/test_eod_fetcher.py`

**Interfaces:**
- Produces: `class S3EodClient(*, endpoint, bucket, region, verify_tls, tls_ca_bundle, addressing_style, auth_mode, max_retries)`, `client.get_object(key: str) -> bytes`

- [ ] **Step 1: Tests for addressing-style path vs virtual** — assert URL starts with `https://files.massive.com/flatfiles/...` (path) vs `https://flatfiles.files.massive.com/...` (virtual); assert signature header differs.
- [ ] **Step 2: Tests for verify_tls / tls_ca_bundle** — assert `requests.Session.verify` is set to `False`, a CA path, or `True` per config.
- [ ] **Step 3: Tests for auth_mode** — assert `Authorization: AWS4-HMAC-SHA256 ...` for sigv4, `Authorization: Bearer ...` for bearer.
- [ ] **Step 4: Tests for retry** — 5xx retried up to `max_retries`, then raises `EodFetchError`.
- [ ] **Step 5: Implement `S3EodClient`** — private `_request(method, key)` that injects SigV4 headers (incl. `x-amz-checksum-mode: ENABLED`), retries with exponential backoff, raises `EodFetchError` on terminal failure.
- [ ] **Step 6: Run tests, confirm PASS.**
- [ ] **Step 7: Live verification against files.massive.com** — fetch `us_stocks_sip/day_aggs_v1/2026/07/2026-07-06.csv.gz` with TEST key; expect 200 + gzip bytes for the 50-symbol universe set.

### Task 3: Parquet partitioned store + SQLite manifest + atomic marker

**Files:**
- Create: `trading_bot/data/data_store.py`
- Test: `tests/test_data_store.py`

**Interfaces:**
- Produces: `class EodDataStore(*, store_root, manifest_db)`, `store.write_partition(symbol, interval, date, df)`, `store.partition_path(symbol, interval, date) -> Path`, `store.mark_fetched(date)`, `store.was_fetched(date) -> bool`

- [ ] **Step 1: Test `write_partition` produces `parquet/{symbol}/{interval}/{YYYY}/{MM}/{date}.parquet`** with a 3-row DF in tmp_path.
- [ ] **Step 2: Test `partition_path` is consistent** — deterministic path, idempotent across calls.
- [ ] **Step 3: Test `manifest_db` records the (symbol, interval, date) row** — verify via SELECT.
- [ ] **Step 4: Test `mark_fetched` writes marker atomically** — uses `os.replace` from tmp file; concurrent reader sees either old or new state.
- [ ] **Step 5: Test `was_fetched` reads the marker** — returns True after mark_fetched, False before.
- [ ] **Step 6: Implement `EodDataStore`** — partition layout, Parquet write via `pyarrow`, SQLite schema:
  ```sql
  CREATE TABLE partitions (
    symbol TEXT, interval TEXT, date TEXT,
    rows INTEGER, written_at INTEGER,
    PRIMARY KEY (symbol, interval, date)
  );
  ```
- [ ] **Step 7: Run tests, confirm PASS.**

### Task 4: Runner orchestration with per-product routing

**Files:**
- Create: `trading_bot/data/eod_runner.py`
- Create: `trading_bot/config/settings.py` (modify — add `EodDataStoreSettings`)
- Test: `tests/test_eod_runner.py`

**Interfaces:**
- Produces: `run_eod_fetch(*, store_root, manifest_db, client, store, date, intervals) -> Result` where `Result` has `.partitions: list[tuple[str, str, str]]` and `.errors: list[tuple[str, str]]`
- Mapping: `_INTERVAL_TO_PRODUCT = {"1d": "day-aggregates", "1m": "minute-aggregates", "quotes": "quotes", "trades": "trades"}` — quotes/trades present so they activate when key is upgraded; defaults stay `[1d, 1m]`
- Helper: `_key_template_for(cfg, product) -> str` — returns `day_aggregates_key_template`, `minute_aggregates_key_template`, `quotes_key_template`, `trades_key_template` respectively

- [ ] **Step 1: Test interval→product routing** — assert `[1d, 1m, quotes, trades]` maps correctly.
- [ ] **Step 2: Test key-template-per-product** — assert `_key_template_for` returns the correct template for all 4 products.
- [ ] **Step 3: Test per-interval failure isolation** — quotes 403 doesn't abort 1m; `result.errors` contains the failed interval.
- [ ] **Step 4: Test marker written only on partial success** — partial success writes marker; total failure does NOT.
- [ ] **Step 5: Test idempotent re-run** — `was_fetched(date)` is True after first run; second call short-circuits and is a no-op.
- [ ] **Step 6: Implement `EodRunner`** (skip if already present — verify exists):
  ```python
  def run_eod_fetch(*, client, store, cfg, date, intervals):
      result = Result()
      products_attempted = []
      for interval in intervals:
          try:
              product = _INTERVAL_TO_PRODUCT[interval]
              key_template = _key_template_for(cfg, product)
              # ... fetch + persist per-interval
              products_attempted.append((interval, "ok"))
          except EodFetchError as e:
              result.errors.append((interval, str(e)))
      if any(s == "ok" for _, s in products_attempted):
          store.mark_fetched(date)
      return result
  ```
- [ ] **Step 7: Run tests, confirm PASS.**

### Task 5: Settings + CLI + .env.example

**Files:**
- Modify: `trading_bot/config/settings.py` — add `EodDataStoreSettings` block
- Modify: `trading_bot/cli/app.py` — add `eod-fetch` typer command
- Modify: `burn-in-config.yaml`, `config.yaml` — add `eod_data_store:` block
- Modify: `.env.example` — add 4 `MASSIVE_S3_*` vars
- Modify: `trading_bot/config/loader.py` — reject api_key/secret strings in YAML

**Interfaces:**
- Produces: `settings.eod_data_store: EodDataStoreSettings`, CLI `eod-fetch --date 2026-07-06 --intervals 1d,1m --dry-run`

- [ ] **Step 1: Test loader rejects hardcoded credentials** — feed it YAML with `api_key: abc` and `secret: xyz`, expect `ConfigError`.
- [ ] **Step 2: Test loader resolves env vars** — `MASSIVE_S3_ACCESS_KEY_ID` → `eod_data_store.s3_access_key_id` after merge.
- [ ] **Step 3: Test `eod-fetch --dry-run`** — monkeypatch `run_eod_fetch`; assert it's called, no marker written, no network.
- [ ] **Step 4: Test `eod-fetch --intervals 1d,1m,quotes,trades`** — assert all 4 passed to `run_eod_fetch`.
- [ ] **Step 5: Implement `EodDataStoreSettings`** (verify exists — should have all 16 fields): enabled, provider, intervals, backfill_years, minute_backfill_years, throttle_seconds, max_retries, store_root, manifest_db, s3_region, verify_tls, tls_ca_bundle, auth_mode, addressing_style, day_aggregates_key_template, minute_aggregates_key_template, quotes_key_template, trades_key_template.
- [ ] **Step 6: Implement CLI command** (verify exists).
- [ ] **Step 7: Run tests, confirm PASS.**

### Task 6: Cron integration in auto-burn-in.sh

**Files:**
- Modify: `scripts/auto-burn-in.sh`
- Modify: `tests/test_auto_burn_in_script.py`

**Interfaces:**
- Produces: shell function `run_eod_data_download()` gated by `[ "${EOD_DATA_STORE:-true}" = "true" ]`, env vars `EOD_DATA_STORE`, `EOD_FETCH_TIME`, `EOD_FETCH_BACKFILL_DAYS`; invoked between `run_discovery "daily"` and `run_nightly_tuning`.

- [ ] **Step 1: Test shell function exists and is env-gated** — parse script, assert `run_eod_data_download` defined; set `EOD_DATA_STORE=false`, assert not invoked.
- [ ] **Step 2: Test invocation order** — assert `run_eod_data_download` is called after `run_discovery "daily"` and before `run_nightly_tuning`.
- [ ] **Step 3: Implement `run_eod_data_download()`**:
  ```bash
  run_eod_data_download() {
    [ "${EOD_DATA_STORE:-true}" = "true" ] || return 0
    local prev_date
    prev_date=$(date -v -1d +%Y-%m-%d)
    log "INFO" "running eod data download for $prev_date"
    if ! "${REPO_ROOT}/tradebot-local" --config-path "${BURN_IN_CONFIG}" eod-fetch \
        --date "$prev_date" \
        --backfill-days "${EOD_FETCH_BACKFILL_DAYS:-3}"; then
      log "WARNING" "eod fetch failed for $prev_date (continuing)"
    fi
  }
  ```
- [ ] **Step 4: Wire into main loop** between `run_discovery "daily"` and `run_nightly_tuning`.
- [ ] **Step 5: Run tests, confirm PASS.**

### Task 7: Downstream consumers (tuning_overrides + daily_supermodel)

**Files:**
- Modify: `trading_bot/learning/tuning_overrides.py` — add `_maybe_nudge_window_from_data_store()`
- Modify: `scripts/daily_supermodel.py` — add data-store coverage logging

**Interfaces:**
- Produces: tuning nudge via `lookback_days *= sqrt(rolling_realised_vol)`; coverage log line `[data-store] partitions=N symbols=K spans=YYYY-MM-DD..YYYY-MM-DD`

- [ ] **Step 1: Test realised-vol heuristic** — feed a synthetic store, verify `lookback_days` scales when vol is high.
- [ ] **Step 2: Test coverage log line format** — capture stdout, assert substring present.
- [ ] **Step 3: Implement `_maybe_nudge_window_from_data_store`** (verify exists).
- [ ] **Step 4: Implement data-store coverage log in daily_supermodel.py** (verify exists).
- [ ] **Step 5: Run tests, confirm PASS.**

---

## Self-Review (post-write)

**Spec coverage:**
- [x] Nightly S3 fetch → covered by Tasks 2, 4, 6
- [x] Parquet store → covered by Task 3
- [x] Per-product routing for 4 products → covered by Task 4 (only 2 active by default)
- [x] Idempotency via marker → covered by Tasks 3, 4
- [x] Auth (sigv4/bearer), addressing (path/virtual), TLS (verify/ca_bundle) → covered by Task 2
- [x] Credentials via env only → covered by Task 5
- [x] Cron integration → covered by Task 6
- [x] Downstream consumers (tuning + supermodel) → covered by Task 7
- [x] All network-free tests → monkeypatch pattern in Tasks 2, 4, 5

**Placeholder scan:** No "TBD", "TODO", "implement later", "fill in details", vague steps.

**Type/method consistency:**
- `S3EodClient.get_object(key: str) -> bytes` used identically in Tasks 2 and 4.
- `EodDataStore.write_partition(symbol, interval, date, df)` signature consistent across Tasks 3 and 4.
- `_INTERVAL_TO_PRODUCT` keys `[1d, 1m, quotes, trades]` consistent between Tasks 4 and 5.
- `_key_template_for(cfg, product)` lookup matches the 4 settings fields in Task 5.

---

## Execution Status

- [x] Task 1: SigV4 reference vectors — test added in `tests/test_eod_fetcher.py::test_sigv4_reference_signature_matches_aws_doc`; passing
- [x] Task 2: S3EodClient — implemented and live-verified (TEST key) against `files.massive.com` bucket `flatfiles`
- [x] Task 3: EodDataStore — implemented, tested, 100 partitions persisted for 2026-07-06 (50 × 1d + 50 × 1m)
- [x] Task 4: Runner orchestration — implemented with per-product try/except, _INTERVAL_TO_PRODUCT supports all 4 products but defaults to `[1d, 1m]`
- [x] Task 5: Settings + CLI + .env.example — implemented, running
- [x] Task 6: Cron integration — `run_eod_data_download()` in `auto-burn-in.sh` between daily discovery and nightly tune
- [x] Task 7: Downstream consumers — `_maybe_nudge_window_from_data_store` in tuning_overrides.py, coverage log in daily_supermodel.py
- [x] Phase 6 quality review — all critical bugs fixed (gzip decode, throttle off-by-one, marker-on-failure, path-style signing, `x-amz-checksum-mode`)
- [x] End-to-end live verification — 100 real OHLCV partitions written for 2026-07-06 with TEST key
- [x] All 2006 tests passing (was 1953 at start)

## Known Limitations (logged, not blockers)

- Test API key has read permission for `day_aggs_v1` and `minute_aggs_v1` only; quotes/trades return 403 (plan-tier).
- Code + config keep quotes/trades as opt-in so a key upgrade activates them with no code change.
- `verify_tls: false` is required for the massive.com S3 endpoint which serves a self-signed cert (endpoint has own subnet/LB). Loud WARNING logged at client construction.
- `.last_eod_fetch_YYYY-MM-DD.marker` is written even when quotes/trades fail (partial-success idempotency).

## Files Touched (final)

- Create: `trading_bot/data/data_store.py`, `trading_bot/data/eod_fetcher.py`, `trading_bot/data/eod_runner.py`
- Modify: `trading_bot/config/settings.py`, `trading_bot/config/loader.py`, `trading_bot/cli/app.py`, `scripts/auto-burn-in.sh`, `scripts/daily_supermodel.py`, `trading_bot/learning/tuning_overrides.py`, `burn-in-config.yaml`, `config.yaml`, `.env.example`
- Tests: 53 new tests across `tests/test_eod_fetcher.py`, `tests/test_data_store.py`, `tests/test_eod_runner.py`, `tests/test_auto_burn_in_script.py`
- Docs: `docs/EOD_DATA_FEATURE.md` updated with verified design + auth findings; `docs/FUTURE_PATTERN_MINING.md` deferred option D

## Execution Handoff

This plan is **already executed in full** in this session. State above reflects what was actually shipped, verified against live `files.massive.com`, and tested (2006 pass).

To resume work in a new session: read this file, run `.venv/bin/python -m pytest -q` to confirm 2006 passing, then `./tradebot-local --config-path burn-in-config.yaml eod-fetch --date $(date -v -1d +%Y-%m-%d) --intervals 1d,1m` to confirm live fetch still works.

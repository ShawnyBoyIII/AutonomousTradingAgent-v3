# EOD Data Pipeline + Nightly Learning Loop

**Status**: ✅ IMPLEMENTED. All 7 phases of feature-dev workflow complete.
**Last updated**: 2026-07-08.
**Tests**: 32 new tests added (data_store, eod_fetcher, eod_runner, burn-in regression); full suite passes (1985 passed).

## What's in the box

| Component | Path | What it does |
|---|---|---|
| Long-term store (cold archive) | `trading_bot/data/data_store.py` | Parquet partitions + SQLite manifest; `read_bars()`, `write_bars()`, `DataStoreManifest` |
| Massive.com S3 client | `trading_bot/data/eod_fetcher.py` | AWS Sig v4 signing, throttled GET with 5xx retry, CSV parsing, universe filter |
| High-level orchestrator | `trading_bot/data/eod_runner.py` | Loads universe, iterates intervals, persists per-symbol, writes marker file |
| Settings block | `trading_bot/config/settings.py::EodDataStoreSettings` | `eod_data_store:` config; Pydantic-validated |
| CLI command | `./tradebot-local eod-fetch [--date YYYY-MM-DD] [--backfill-days N] [--dry-run]` | Idempotent, gated on `eod_data_store.enabled` |
| Shell integration | `scripts/auto-burn-in.sh::run_eod_data_download` | Mirrors `run_nightly_tuning`; idempotent via marker file |
| Learning-loop wiring (light) | `tuning_overrides.py::_maybe_nudge_window_from_data_store` | Realised-vol heuristic nudges `strategy_tracker.window` up |
| RL inventory logging | `scripts/daily_supermodel.py` | Logs data-store coverage for top-10 training symbols before training |
| Credentials | `.env.example` + 4 `MASSIVE_S3_*` env vars | Loaded by `python-dotenv` at startup; rejected by `_validate_credentials_not_in_config` if leaked into YAML |

## How to use

```bash
# One-time: put credentials in .env
echo "MASSIVE_S3_ENDPOINT=https://your-endpoint" >> .env
echo "MASSIVE_S3_ACCESS_KEY_ID=..." >> .env
echo "MASSIVE_S3_SECRET_ACCESS_KEY=..." >> .env
echo "MASSIVE_S3_BUCKET=..." >> .env

# Manual run (after massive.com publishes, ~11:00 ET):
./tradebot-local eod-fetch                                  # yesterday
./tradebot-local eod-fetch --date 2026-07-07               # specific date
./tradebot-local eod-fetch --backfill-days 5                # catch-up
./tradebot-local eod-fetch --dry-run                       # plan only

# Automatic (inside auto-burn-in.sh, runs after daily discovery):
# Gated on EOD_FETCH_TIME (default 11:30) and a per-day marker file.
# Set EOD_DATA_STORE=false to opt out.
./scripts/auto-burn-in.sh
```

## Architectural decisions captured

- **Storage**: Parquet partitions at `state/data_store/parquet/{SYMBOL}/{interval}/{YYYY}/{MM}/{YYYY-MM-DD}.parquet`. Manifest at `state/data_store.db` (separate from `state/market_data_cache.db` — the live hot cache is untouched, per the user's Q2.i answer).
- **S3 client**: Local AWS Sig v4 implementation (~50 lines of `hmac`/`hashlib`). No `boto3` dependency. Verified against the official AWS reference test vector (`5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7`).
- **S3 file naming**: `stocks/day-aggregates/YYYY-MM-DD.csv` and `stocks/minute-aggregates/YYYY-MM-DD.csv.gz`. If the real bucket uses a different scheme, override via `MassiveFlatFilesClient.s3_key_for` (or edit `build_s3_key`).
- **Universe**: `state/universe.txt` (post-discovery merged list, ~66 symbols) — matches what `$SYMBOLS` already holds in `auto-burn-in.sh`.
- **Backfill window**: 5y daily + 1y minute (Stocks Starter plan limit).
- **Intervals**: `1d` + `1m` per the user's Q4 ("everything").

## Open follow-ups (not blockers)

- **Pattern mining (Option D)**: deferred to `docs/FUTURE_PATTERN_MINING.md`.
- **RL env integration**: `daily_supermodel.py` currently logs data-store coverage but doesn't bypass `fetch_bars`. The deeper wiring (have the RL env prefer `read_bars()` over network when the store has fresh data) is a Phase 6+ follow-up that needs careful treatment of freshness semantics.

---

## Feature summary

At the end of each trading day, download OHLCV flat-files from massive.com's S3
bucket for every symbol in today's scout universe, store them in a **separate
long-term store** (does NOT touch the live hot cache at `state/market_data_cache.db`),
then run two existing learning pipelines against the freshly stored data:

- **(B) Nightly auto-tune** — feed the data through `trading_bot/learning/tuning_overrides.py`
  (the `tune` CLI) so supermodel thresholds drift to match recent performance.
  Writes `state/tuning_overrides.yaml`, consumed by `config/loader.py` on next start.
- **(C) Nightly RL retrain** — extend `scripts/daily_supermodel.py` to train a PPO
  agent on freshly stored data. Writes `state/rl_logs/supermodel/`.

Option (D) — pattern mining — is **deferred**. See `docs/FUTURE_PATTERN_MINING.md`.

The user's mental model: "instead of repulling every time, store the data in the
backend; the backend then studies itself and models the bot to trade better."

---

## User decisions (verbatim, 2026-07-07)

| # | Question | Answer |
|---|---|---|
| Q1 | Scope of "study + model" | **B (auto-tune) + C (RL retrain)**. D (pattern mining) deferred to future enhancement file. |
| Q2 | Cache strategy | **(i) Separate long-term store**, do NOT touch the live hot cache. Intraday scans stay fresh. |
| Q3 | Storage format | "Lean on you for recommendation" — see "Open design decisions" below. |
| Q4 | Intervals to fetch | **Everything** the scan path uses: daily `1y/1d` + intraday `5d/5m` + hourly. Cleanup later if needed. |
| Q5 | Data source | **Massive.com S3 flat-files** (not Alpaca). See "Massive.com research" below. |
| Q6 | Trigger mechanism | **(1)** `run_eod_data_download()` shell function inside `auto-burn-in.sh`, plus a new `./tradebot-local eod-fetch` CLI command. Mirror the `run_nightly_tuning` pattern. |
| Q7 | Backfill window | "You decide." See "Open design decisions" below. |
| Q8 | Symbol universe | **Full scout snapshot** (`state/universe_candidates.json`, ~187 symbols). "Plan is to build a database universe for model training." |
| Q9 | Rate limits / throttling | "Pulls in breaks to not hit limits. You can build what's best." |
| Q10 | Default state | **On by default** in burn-in config. |
| Q11 | What changes tomorrow morning | B and C run automatically after the EOD fetch. |

---

## Codebase context to rebuild (re-read these 4 files)

1. `trading_bot/data/cache.py` — the existing `MarketDataCache`. SQLite at
   `state/market_data_cache.db`, WAL mode, JSON-in-TEXT serialization.
   TTL table: `5m→150s, 1h→30min, 1d→12h, 1w→24h, 1mo→7d, 1y→30d`.
   **Our new store is separate** — does not modify this.
2. `trading_bot/runtime/session.py` — `should_eod_exit(now, settings)`.
   The only EOD detector today. Fires weekday ≥ 15:55 ET (configurable).
3. `trading_bot/config/settings.py` — Pydantic `Settings` tree. Add a new
   `EodDataStoreSettings` block here following the `SessionSettings` pattern.
4. `scripts/auto-burn-in.sh` — the main loop. Mirror `run_nightly_tuning()`
   (lines ~407–425) and `run_advisory_learner()` (lines ~428–439) for the
   new `run_eod_data_download()` function. Pattern: capture stdout to
   `$LOG_DIR/<name>.log`, never `exit 1` on failure, return 0.

### Additional context (read only if implementing that piece)

- `trading_bot/learning/tuning_overrides.py` — the `tune` CLI input/output contract.
- `trading_bot/advisory/learner.py::_analytics_metrics_by_symbol` — reads
  `scan_features` + `trades` tables grouped by ticker. Pattern to copy for
  the new study job.
- `scripts/daily_supermodel.py` — the existing nightly PPO retrain pipeline,
  the **shape template** for the new EOD job (multi-step, dry-run flag,
  writes `pipeline_result.json`).
- `trading_bot/db/models.py::ScanFeature` — the de-facto feature store,
  already populated by `runtime/orchestrator.py::_persist_scan_feature_row`.
  Our EOD study job READS this; the running bot already writes it.
- `trading_bot/data/market_data.py::fetch_bars` — the existing fetch entry
  point. S3-flat-files fetcher should be a NEW provider or a sibling module,
  not bolted into `fetch_bars` (which has provider-failover semantics meant
  for live trading).
- `config.yaml` and `burn-in-config.yaml` — config files to extend with a
  new `eod_data_store:` block.

---

## Massive.com research (the external data source)

### What we're using

Two flat-file products (CSV, daily S3 download):

| Product | S3 path | Granularity | History | Plan required |
|---|---|---|---|---|
| **Stocks · Day Aggregates** | `S3 /stocks/day-aggregates` | per-day OHLCV + transactions | back to 2003-09-10 | Stocks Starter+ |
| **Stocks · Minute Aggregates** | `S3 /stocks/minute-aggregates` | per-minute OHLCV + transactions | back to 2003-09-10 | Stocks Starter+ |

Both update **at 11:00 AM ET** to include the previous day — this is when the
EOD job should fire (after 11:00 ET day T+1, to fetch day T's bars).

### Flat-file schema (both products)

```
ticker        string   exchange symbol
volume        number
open          number
close         number
high          number
low           number
window_start  integer  Unix nanosecond timestamp (start of aggregate window)
transactions  integer  trade count in window
```

Sample row (day aggregates):
```
BCC | 248274 | 61.68 | 61.99 | 62.565 | 61.41 | 1680033600000000000 | 4073
```

Sample row (minute aggregates):
```
MSFT | 1975 | 276.75 | 275.52 | 276.75 | 275.25 | 16799904000000000 | 83
```

### Plan tier table (for the user's morning upgrade)

| Plan | Day Aggregates | Minute Aggregates | History | Recency |
|---|---|---|---|---|
| Stocks Basic | — | — | 2y | EOD |
| **Stocks Starter** | ✓ | ✓ | 5y | EOD |
| Stocks Developer | ✓ | ✓ | 10y | EOD |
| Stocks Advanced | ✓ | ✓ | All | Real-time |
| Stocks Business | ✓ | ✓ | All | Real-time |

**Recommendation**: Stocks Starter ($?) is the minimum viable tier.
Developer/Advanced if they want longer history for RL training.

### Auth (confirmed 2026-07-08)

User placed 4 env vars in `.env` (gitignored, loaded via `python-dotenv`):

```
MASSIVE_S3_ENDPOINT=<S3-compatible endpoint URL>
MASSIVE_S3_ACCESS_KEY_ID=<access key>
MASSIVE_S3_SECRET_ACCESS_KEY=<secret key>
MASSIVE_S3_BUCKET=<bucket name>
```

These are read at runtime via `os.environ.get(...)`, matching the existing
pattern used by `alpaca_provider.py` and `polygon_provider.py`.

**S3 client**: `boto3` is NOT installed in the venv. The fetcher needs either:
- `pip install boto3` (standard, well-tested, ~40MB) — **recommended**
- Or a lightweight Sig v4 implementation with `requests` (~50 lines of code)

**File naming**: not yet confirmed from the massive.com quickstart (JS-rendered).
The fetcher should auto-discover the naming convention by listing the bucket
prefix `stocks/day-aggregates/` on first run, then cache the pattern.

### Auth — real-world findings (2026-07-08, final)

After the user provided test API keys and we probed the actual massive.com
flat-files endpoint, the working configuration was:

- **Endpoint**: `https://files.massive.com`
- **Bucket**: `flatfiles` (the public alias in the docs IS the real bucket
  for subscribers; `production-flatfiles` is a different internal bucket)
- **Path layout**: `us_stocks_sip/<dataset>/<year>/<month>/<date>.csv.gz` —
  **month is its own directory level** (not zero-padded into the year
  dir). Example: `us_stocks_sip/day_aggs_v1/2026/07/2026-07-06.csv.gz`.
- **Auth**: AWS SigV4 — **with the `x-amz-checksum-mode: ENABLED` header**.
  This is a newer S3 feature (post-2024) that massive.com's gateway
  requires. Without it, the gateway returns the REST API's
  `{"error":"API Key was not provided"}` envelope.
- **Addressing**: path-style (`https://files.massive.com/<bucket>/<key>`).
  Virtual-hosted (`https://<bucket>.files.massive.com/<key>`) gets routed
  by the load balancer to the REST API gateway, which rejects SigV4.
- **TLS**: self-signed certificate. Set `verify_tls: false` in config.

The final, working `eod_data_store` block in `burn-in-config.yaml` is:

```yaml
eod_data_store:
  enabled: true
  provider: massive_flat_files
  intervals: [1d, 1m]
  backfill_years: 5
  minute_backfill_years: 1
  throttle_seconds: 0.2
  max_retries: 3
  store_root: state/data_store
  manifest_db: state/data_store.db
  s3_region: us-east-1
  verify_tls: false
  tls_ca_bundle: null
  auth_mode: sigv4
  addressing_style: path
  day_aggregates_key_template: "us_stocks_sip/day_aggs_v1/{year}/{month}/{date}.csv.gz"
  minute_aggregates_key_template: "us_stocks_sip/minute_aggs_v1/{year}/{month}/{date}.csv.gz"
```

### End-to-end verification (with test API key)

```bash
$ ./tradebot-local --config-path burn-in-config.yaml eod-fetch --date 2026-07-06 --intervals 1d
2026-07-07 21:27:44 WARNING trading_bot.data.eod_fetcher:230 TLS certificate verification DISABLED...
2026-07-07 21:27:56 INFO trading_bot.data.eod_runner:230 eod fetched product=day-aggregates interval=1d date=2026-07-06 symbols=50 rows=50
eod-fetch=2026-07-06 partitions=50
eod-fetch total_partitions=50

$ find state/data_store -type f | head
state/data_store/.last_eod_fetch_2026-07-06.marker
state/data_store/parquet/AAPL/1d/2026/07/2026-07-06.parquet
state/data_store/parquet/AMZN/1d/2026/07/2026-07-06.parquet
state/data_store/parquet/PLTR/1d/2026/07/2026-07-06.parquet
... (50 partitions total, one per universe symbol)
```

### What changed in the code (final)

- `EodDataStoreSettings.addressing_style` default changed from `"virtual"` to
  `"path"` (massive.com routes virtual-hosted to REST API gateway).
- `MassiveFlatFilesClient._signing_key_headers` now includes
  `x-amz-checksum-mode: ENABLED` in the canonical signed headers
  (required by massive.com's gateway).
- `day_aggregates_key_template` / `minute_aggregates_key_template` defaults
  in `burn-in-config.yaml` updated to include `{month}/` directory level
  (verified against actual bucket layout).

### Boto3 reproducer (works for the same endpoint)

```python
import boto3, gzip
from botocore.config import Config

session = boto3.Session(
    aws_access_key_id=os.environ["MASSIVE_S3_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["MASSIVE_S3_SECRET_ACCESS_KEY"],
)
s3 = session.client(
    "s3",
    endpoint_url="https://files.massive.com",
    config=Config(signature_version="s3v4"),
    verify=False,
)
key = "us_stocks_sip/day_aggs_v1/2026/07/2026-07-06.csv.gz"
obj = s3.get_object(Bucket="flatfiles", Key=key)
body = gzip.decompress(obj["Body"].read())
print(body[:200])  # b'ticker,volume,open,close,high,low,window_start,transactions\n...'
```

### Alternative: REST fallback

If S3 access proves awkward, Massive.com's REST endpoint
`GET /v2/aggs/grouped/locale/us/market/stocks/{date}` returns the same
OHLCV data for all U.S. stocks in one JSON response per day. Schema:
`results[].T` (ticker), `.o .h .l .c .v .vw .n .t` (open/high/low/close/
volume/vwap/transactions/ms-timestamp). Useful as a per-day bulk fetch
when S3 is unavailable, but more bandwidth than CSV.

Docs: https://massive.com/docs/rest/stocks/aggregates/daily-market-summary.md

---

## Open design decisions (resolve in Phase 4)

### D1. Storage format (Q3 was "you decide") — RECOMMENDATION

**Recommendation**: Parquet files at `state/data_store/{symbol}/{interval}/YYYY/MM/DD.parquet`,
with a thin SQLite manifest at `state/data_store.db` for queryability and
idempotency checks (last-fetched date per symbol/interval).

**Rationale**:
- Parquet is columnar — fast for the analytical reads the study/RL jobs do
  (slice one symbol's close prices over 5y, vectorized).
- SQLite manifest mirrors the existing `state/market_data_cache.db` pattern
  in this repo, so the codebase stays consistent.
- The live `fetch_bars` hot cache is untouched; this is a cold, append-only archive.

**Counter-argument**: if the team prefers SQLite everywhere for simplicity,
the data could go directly into a new table in a new SQLite DB at
`state/data_store.db`. Slower for analytics, simpler stack. This is the
"pragmatic balance" approach for the architect sub-task to evaluate.

### D2. Backfill window (Q7 was "you decide") — RECOMMENDATION

**Recommendation**: 5 years of daily bars, 1 year of minute bars. Matches
the Stocks Starter plan's history limit exactly (no over-fetch).
First-run backfill: ~66 symbols × 5y × 252 trading days = ~83k daily rows,
trivial. Minute bars: ~66 × 252 × 390 = ~6.5M rows/year, ~50 MB Parquet/year
per symbol — manageable.

### D3. Bulk vs per-symbol fetch strategy

S3 flat-files are **one file per day containing all U.S. tickers** (not one
file per symbol). So the EOD job downloads ONE big file per day and filters
to our universe when materializing into Parquet. This is the killer feature
of the flat-files approach: O(days) requests instead of O(symbols × days).

For backfill of N years: ~252 files/year × 5 years = 1,260 day-aggregate
files + 1,260 minute-aggregate files. Sequential download with 5 req/sec
backoff = ~10 minutes total. Trivial.

### D4. Throttling

User said "pulls in breaks". Concretely: process files sequentially with
`time.sleep(0.2)` between S3 GETs (5 req/sec). Exponential backoff on any
HTTP 429 / 5xx, capped at 60-second retry. Mirror the existing retry
pattern in `trading_bot/data/providers/polygon_provider.py`.

### D5. Scheduling inside `auto-burn-in.sh`

Two natural slots:

- **(a) Daily 11:00 ET slot, after Massive.com publishes yesterday's bars** —
  mirrors `run_discovery`. Wait until ~11:30 ET to give Massive.com a buffer,
  then fire `run_eod_data_download()`. Idempotent via `.last_eod_fetch_date`
  marker file (mirror the `.last_discover_date` pattern).
- **(b) Nightly slot after `run_nightly_tuning`** — combined with the learning
  pass. Workflow: discover → eod-fetch → tune → retrain-rl → advisory-learn.

Recommendation: **(b)** — run the fetch BEFORE the nightly tune/retrain so the
learning loops consume fresh data.

---

## Phase 4 input (the spec to give to architect sub-tasks)

When dispatching the 3 architect sub-tasks tomorrow, give each this brief:

> Design the implementation of an EOD data-pipeline + nightly learning loop
> for the Autonomous Trading Agent. Requirements:
>
> 1. **EOD fetcher** at `trading_bot/data/eod_fetcher.py` (new module).
>    Downloads daily + minute S3 flat-files from massive.com for the full
>    scout universe (`state/universe_candidates.json`, ~187 symbols).
>    Filters to our symbols when materializing.
>
> 2. **Long-term store** separate from the live hot cache. See open decision D1
>    for the format trade-off.
>
> 3. **CLI command** `./tradebot-local eod-fetch [--backfill 5y] [--dry-run]`
>    mirroring the `tune` CLI shape.
>
> 4. **Shell integration** in `scripts/auto-burn-in.sh` as
>    `run_eod_data_download()` function, mirroring `run_nightly_tuning()`.
>    Gated by `EOD_DATA_STORE_ENABLED` env var (default `true`).
>    Idempotent via `.last_eod_fetch_date` marker.
>
> 5. **Wire the stored data into the existing learning loops**:
>    - `trading_bot/learning/tuning_overrides.py::propose_tuning_overrides`
>      reads from the new store (in addition to existing inputs).
>    - `scripts/daily_supermodel.py` reads from the new store for training data.
>
> 6. **Config** in `burn-in-config.yaml`:
>    ```yaml
>    eod_data_store:
>      enabled: true           # on by default per user Q10
>      provider: massive_flat_files
>      intervals: ["1d", "1m"]  # Q4: "everything"
>      backfill_years: 5
>      minute_backfill_years: 1
>      throttle_seconds: 0.2
>    ```
>
> Constraints:
> - Never touch `state/market_data_cache.db` (live hot cache).
> - Never modify `trading_bot/data/market_data.py::fetch_bars` (live-trading path).
> - Match the codebase's existing shell-function pattern in `auto-burn-in.sh`.
> - Paper-only safety envelope (no live trades triggered by the learning loop).

The three sub-task focuses should be:
1. **Minimal changes** — smallest diff, maximum reuse of `daily_supermodel.py`
   and `tuning_overrides.py`.
2. **Clean architecture** — new `trading_bot/data_store/` package, abstraction
   for "long-term store backend" so Parquet can be swapped for SQLite later.
3. **Pragmatic balance** — single new module, Parquet + SQLite manifest, no
   new abstractions beyond what's needed.

---

## User's stated goals for tomorrow

> "I want B and C" (auto-tune + RL retrain).

After Phase 4 designs are presented and the user picks an approach, Phase 5
implementation begins. Suggested file plan (final, may shift per architecture):

- `trading_bot/data/eod_fetcher.py` — new module, S3 download + filter + persist
- `trading_bot/data_store/` — new package (parquet_writer, manifest, reader)
- `trading_bot/data_store/__init__.py`
- `trading_bot/data_store/parquet_store.py`
- `trading_bot/data_store/manifest.py` — SQLite manifest, idempotency
- `trading_bot/cli/app.py` — add `eod-fetch` command (mirror `tune` shape)
- `trading_bot/config/settings.py` — add `EodDataStoreSettings`
- `config.yaml` + `burn-in-config.yaml` — add `eod_data_store:` block
- `scripts/auto-burn-in.sh` — add `run_eod_data_download()` function
- `trading_bot/learning/tuning_overrides.py` — extend to read from new store
- `scripts/daily_supermodel.py` — extend to read training data from new store
- `tests/test_eod_fetcher.py` — new (network-free, monkeypatch S3 client)
- `tests/test_auto_burn_in_script.py` — extend with regression test for
  `run_eod_data_download` integration

---

## What's done so far

- Phase 1: Discovery — confirmed user intent.
- Phase 2: Codebase exploration — 3 sub-tasks completed (market data, EOD/
  universe, learning stack). All findings captured above.
- Phase 3: Clarifying questions answered by the user (see table above).
- Massive.com product research (flat-files for stocks, plan tiers).

## What's not done

- Phase 4: architecture design (3 approaches via architect sub-tasks).
- Phase 5: implementation.
- Phase 6: quality review.
- Phase 7: summary.

## What we cannot verify yet

- The exact S3 bucket name and file-naming convention for Massive.com's
  flat-files. The docs page at `https://massive.com/docs/flat-files/quickstart`
  did not return useful content for automated fetch (likely JS-rendered or
  sign-in-gated). The user will read it manually after upgrading their plan
  and report back; update this file's "Auth" section with concrete values.

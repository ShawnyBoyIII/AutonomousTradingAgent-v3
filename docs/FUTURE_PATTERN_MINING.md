# Future Enhancement: Pattern Mining Pass (Option D)

**Status**: Deferred. The user chose B (auto-tune) + C (RL retrain) for the
immediate EOD-data-pipeline work; D is recorded here for a later session.

Parent context: see `docs/EOD_DATA_FEATURE.md` for the EOD pipeline this
enhancement would extend.

---

## What "D" means

Once the EOD data store is populated nightly (option B and C are wired),
introduce a **new pattern-mining pass** that scans the stored OHLCV data for
repeating setups and anti-patterns, then writes findings to a store the
scanner can read at runtime to bias signal generation.

Distinct from B/C:
- **B (auto-tune)** nudges allowlisted thresholds from realized P&L attribution.
- **C (RL retrain)** fits a model end-to-end.
- **D (pattern mining)** surfaces *structural* observations the model can't
  learn from rewards alone — e.g. "AAPL gaps down 0.4% on Mondays 71% of the
  time after a 3-up week", "NVDA never holds breakouts above 2× ATR in the
  last hour when VIX > 20".

## Scope options (pick one when resurrecting)

| Option | Output | Wired into runtime as |
|---|---|---|
| D-light | A `state/patterns/digest.json` printed nightly + surfaced in the dashboard. Operator reads it manually. | Read-only report. |
| D-medium | Plus `state/patterns/symbol_overrides.yaml` (promote/avoid lists, similar to `state/advisory_learner/scout_override.yaml`). | Auto-applied by the scanner via a `apply_pattern_override()` sibling of `apply_scout_override()`. |
| D-heavy | Plus pattern features written into the `scan_features` table (per-symbol, per-pattern columns). | The strategy layer can gate signals on these features. |

Start with D-light; only escalate to D-medium after the digest proves useful
in manual review.

## Where it lives in the codebase

Closest existing primitives (do NOT reimplement):
- `trading_bot/factors/` — alpha factor zoo (Qlib, Kakushadze, GTJA, Academic)
  with IC/IR benching (`factors/bench.py`). Pure compute from OHLCV. Standalone
  library today; not wired into the running bot. This is the natural compute
  layer for a miner.
- `trading_bot/research/store.py` — SQLite hypothesis/experiment store
  (`state/research.db`). The pattern miner's findings would persist here.
- `trading_bot/memory/` — FTS5 research memory store with a `MemoryRetriever`
  that exposes `recall_for_context`, `store_research_finding`,
  `store_trading_insight`. Already integrated with the research autopilot.

## Suggested skeleton when this gets resurrected

```
trading_bot/patterns/
├── __init__.py
├── miner.py            # the nightly job — reads data_store, runs factors, writes findings
├── digest.py           # the daily digest renderer (markdown + JSON)
└── runtime_reader.py   # mirrors advisory/learner.py apply_scout_override pattern
```

Hook into `scripts/auto-burn-in.sh` as a sibling of `run_advisory_learner()`:
named `run_pattern_miner()`, env-gated by `PATTERN_MINER_ENABLED=false` by
default (unlike the EOD fetcher, this is **off** until proven in).

## Inputs

- `state/data_store/` Parquet files (after B+C work lands) — the OHLCV history.
- `state/burn_in.db` `trades` table — for labeling which patterns actually
  made money (joins a pattern's "active" days with the bot's entry/exit P&L).
- `state/burn_in.db` `scan_features` table — for contextual regime info.

## Outputs (minimum viable)

- `state/patterns/digest.md` — top-N patterns with stats: hit rate, avg move,
  sample size, last-seen date.
- `state/patterns/digest.json` — same data machine-readable.
- Append to `state/research.db` as hypotheses for later scoring.

## Why defer now

- The data store it reads from does not exist yet (B+C work is the prerequisite).
- Pattern mining is open-ended — it's easy to burn weeks tuning thresholds on
  noise. Better to wait until B/C have run for a few weeks and we have a
  baseline P&L + a stable feature store, then mine *against* that baseline.
- The advisory learner already covers the "which symbols to trade" question;
  pattern mining overlaps unless we're sure we want a separate stream.

## Resurrection checklist

When picking this back up:

1. Confirm the EOD data store from `docs/EOD_DATA_FEATURE.md` has been live
   for ≥ 4 weeks (need real data to mine, not synthetic).
2. Confirm B (auto-tune) has produced at least one `state/tuning_overrides.yaml`
   cycle so we know the feedback loop closure works end-to-end.
3. Read `trading_bot/factors/bench.py` and `trading_bot/research/store.py`
   fresh — they may have evolved.
4. Pick a scope option from the table above (recommend D-light).
5. Dust off this file and convert it into a feature-dev workflow of its own.

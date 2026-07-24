# Remediation Plan — Trading Bot Loose Ends

> **Historical.** Most items below were addressed during the
> July 2026 cleanup passes (recovery branch hardening and
> application reachability review). This file is retained as a
> change-log reference.
>
> Current operational reality lives in **`AGENTS.md`**. New defects
> and follow-ups belong in the SDD progress ledger or as
> follow-up commits — not here.

Status legend: [DONE] completed · [READY] actionable now · [DESIGN] needs design decision first · [WATCH] monitor, low priority

---

## P0 — Correctness / state-consistency bugs (fix first)

### 1. [DONE] `db/session.py` ignores `settings.app.state_db_path`
**Problem:** `_resolve_db_path` derived the SQLAlchemy DB path from `log_dir.parent/"state"/"trading_bot.db"` while every other consumer (PortfolioLedger at ~30 sites) uses `settings.app.state_db_path`. They coincided only under default config; a custom `state_db_path` or `log_dir` silently split writes across two files.
**Impact:** `trades`/`positions`/`scan_features` SQLA tables could land in a different DB file from the `orders` sqlite3 table, breaking cross-table reporting and circuit-breaker consistency.
**Fix applied:** `trading_bot/db/session.py:13` now returns `Path(settings.app.state_db_path).resolve()`, matching `PortfolioLedger`. Verified: 1997 passing (1 pre-existing RL failure unrelated).
**Effort:** done — 1 line.

### 2. [DONE] Dual-write to `orders` + `trades` without a transaction
**Problem:** Every BUY/SELL persists via two independent writes: `ledger.record_fill` (raw sqlite3, `orders` table) and `upsert_trade`/`update_trade_exit` (SQLAlchemy, `trades` table). A crash between them leaves the tables divergent.
**Impact:** `get_consecutive_losses` reads `orders`; PF reports read `trades`. Same closed trade can show different P&L in each view; circuit breaker's loss count drifts from attribution.
**Fix approach:** Route both writes through a single transactional boundary. Either (a) make `upsert_trade` a no-op if `record_fill` failed (gate on a shared return code), or (b) collapse to one persistence layer. Lowest-risk: have `_persist_trade_to_db` and `position_exit.py:48-91` check `record_fill`'s success first and skip the SQLA write on failure (rather than silently swallowing). Stop swallowing exceptions at `orchestrator.py:2591` — log them.
**Files:** `trading_bot/runtime/orchestrator.py:2541-2592`, `trading_bot/runtime/position_exit.py:48-91`, `trading_bot/portfolio/ledger.py:173`.
**Effort:** M (design the shared error contract + tests under failure injection).

### 3. [DESIGN] PF<0.8 confidence gate is not enforced in Python
**Problem:** AGENTS.md says "confidence gates auto-halt at PF < 0.8 after 50+ trades." But `circuit_breaker.py` has zero PF logic (verified by grep). The gate lives only in `monitoring/realtime_pnl.py:316-332` (alert-only) and `scripts/auto-burn-in.sh:714-718` (shell `exit 1`). Running `paper-trade` via CLI bypasses the gate entirely.
**Impact:** A bot running outside the burn-in shell can keep trading past PF<0.8. The only Python-enforced halts are consecutive losses (5) and drawdown (10%).
**Fix approach:** Decide the contract first — should PF<0.8 engage the kill switch (true halt) or remain advisory? If halt: add a `check_pnl_confidence_gate` call to `circuit_breaker.py` that reads realized P&L from the ledger and calls `halt_trading(ledger, reason="confidence_gate_pf", triggered_by=CIRCUIT_BREAKER)` when PF<0.8 AND trade_count≥50. If advisory: update AGENTS.md to say "alert-only" to stop the overstatement.
**Files:** `trading_bot/safety/circuit_breaker.py:21`, `trading_bot/monitoring/realtime_pnl.py:315`, `trading_bot/portfolio/ledger.py` (add a `compute_profit_factor` method).
**Effort:** S-M (PF computation already exists in `monitoring/realtime_pnl.py`; just wire it through the breaker).

### 4. [DONE] Two divergent `manage-positions` implementations
**Problem:** `cli/app.py:603` `_run_manage_positions_once` and `runtime/continuous_loop.py:159` `_run_manage_positions_once` (docstring admits "Mirrors…") diverge: (a) CLI ratchets trailing stop then `continue`s (exits next cycle); continuous-loop exits immediately; (b) CLI evaluates counter-thesis against the held position; continuous-loop gates on `if signal is not None` (today's fresh scan) and skips exit if today produced no signal.
**Impact:** Trailing-stop exits are delayed in CLI mode. Counter-thesis broken-thesis exits are skipped in continuous-loop mode whenever today's scan is empty. Same command name, different risk exposure.
**Fix approach:** Delete one and have both CLI and continuous-loop call the same function. Keep `cli/app.py`'s version as canonical (it handles counter-thesis correctly against the held position, which is the ADR-001 intent). Have `continuous_loop.py` import and call it. Make the trailing-stop exit immediate in the unified function.
**Files:** `trading_bot/cli/app.py:603`, `trading_bot/runtime/continuous_loop.py:159`.
**Effort:** M (de-duplicate carefully — the two share helpers but differ in session/loop plumbing).

### 5. [DONE] Counter-thesis fetches unvalidated bars, and runs twice per cycle
**Problem:** `fetch_counter_thesis_context` (`counter_thesis.py:141-150`) uses `market_data.fetch_bars` instead of `fetch_and_validate_bars`, bypassing V2.5 fail-fast validation. Also `_evaluate_counter_thesis_for_signal` (`orchestrator.py:2292`) and `_evaluate_counter_thesis_for_position` (`orchestrator.py:2309`) each fetch daily+intraday bars independently per symbol.
**Impact:** Bad data silently flows into counter-thesis checks; mitigated only by the "None context = no block" fallback. Double-fetch doubles network calls during scan + manage-positions.
**Fix approach:** Switch `fetch_counter_thesis_context` to `fetch_and_validate_bars`. Cache the counter-thesis context per symbol per `run_scan`/`run_paper_trade`/`_run_manage_positions_once` invocation and pass it to both the signal and position evaluators.
**Files:** `trading_bot/strategy/counter_thesis.py:127-165`, `trading_bot/runtime/orchestrator.py:2277-2360`.
**Effort:** S-M.

---

## P1 — Latent bugs / silent drift

### 6. [DONE] Portfolio heat frozen per `run_paper_trade` invocation
**Problem:** `portfolio_heat` computed once at `orchestrator.py:766` before the symbol loop; never refreshed as fills mutate `state.equity`/`cash` inside the loop.
**Impact:** The 6th fill's heat check uses stale heat from before fills 1-5.
**Fix approach:** Move `portfolio_heat = _calculate_portfolio_heat(state, settings)` inside the per-symbol loop (or recompute when `state` is replaced at `orchestrator.py:1139`).
**Files:** `trading_bot/runtime/orchestrator.py:766, 979`.
**Effort:** S.

### 7. [READY] Divergent ATR multipliers understate dollar_risk
**Problem:** Position sizer uses `atr_multiplier=2.0` (`risk_manager.py:109`) for `dollar_risk = atr × atr_multiplier`. Signal stop placement uses `atr_stop_multiplier=3.0` (`strategy_selector.py:368`, `intraday_signal_engine.py:66`). So `signal.stop_loss` is farther from entry than the sizer's `dollar_risk` assumes.
**Impact:** `dollar_risk` understates the true stop distance; risk-per-trade accounting is off by ~33%.
**Fix approach:** Either (a) feed `signal.stop_loss` into the sizer's `calculate_fixed_stop_position_size` (which uses real `entry - stop` distance) by default, or (b) document that `atr_multiplier` is a sizing model and `atr_stop_multiplier` is a stop model and they're intentionally separate. Pick one and codify.
**Files:** `trading_bot/risk/position_sizer.py:6`, `trading_bot/risk/risk_manager.py:99-120`.
**Effort:** S-M (decision + tests).

### 8. [DONE] `min_stop_distance_pct` defaults to 0.0
**Problem:** `settings.py:114` defaults to `0.0`; AGENTS.md mandates "5% minimum stop distance on 5-minute bars." Rule only activates if `burn-in-config.yaml` sets it. Any path run without that config silently disables the noise protection.
**Fix approach:** Either change the default to `3.0`/`5.0` (matching the AGENTS.md mandate), or assert at loader time that burn-in configs override it. Default change is safer; add a test that enforces it.
**Files:** `trading_bot/config/settings.py:114`, `trading_bot/strategy/strategy_selector.py:377`.
**Effort:** S.

### 9. [WATCH] Confidence-scale mismatch in parallel BUY-vote tiebreak
**Problem:** `orchestrator.py:1311` `max(buy_votes, key=confidence)` lets V2.5 (0.75/0.8/0.9) systematically dominate V3 (0/0.3/0.55/0.75/0.9) when both BUY.
**Impact:** Attribution skews V2.5 even when V3 is more regime-aware; the V3 setup is silently dropped.
**Fix approach:** Normalize confidence to a 0-1 scale per source before tiebreak, or use a domain-aware comparator (e.g. prefer V3 when regime is non-trending). Needs a design discussion — leave for now.
**Files:** `trading_bot/runtime/orchestrator.py:1311`.
**Effort:** M.

### 10. [DONE] `realized_pnl=-fees` only on BUYs; `unrealized_pnl=0.0` hardcoded
**Problem:** `_portfolio_state_from_broker` (`orchestrator.py:2170-2171`) sets `realized_pnl = previous.realized_pnl - fill_fees` and `unrealized_pnl = 0.0`. Equity snapshots post-BUY never reflect open-position mark-to-market until the next `manage-positions` tick.
**Impact:** `equity_history` rows chronicle cash-realized P&L, not mark-to-market. Reporting between trades is misleading.
**Fix approach:** Compute `unrealized_pnl` from `positions × (last_price - average_cost)` in `_portfolio_state_from_broker` using the fill's entry price (conservative — no extra fetch).
**Files:** `trading_bot/runtime/orchestrator.py:2139-2172`.
**Effort:** S-M.

### 11. [READY] Partial take-profits skip `trades` table write
**Problem:** `fill_partial_take_profit_position` (`position_exit.py:141-172`) calls `fill_sell_position` with `close_db_position=False, mark_exit_timestamp=False`. The DB block at `position_exit.py:68/85` is gated, so partial SELLs are written to `orders` and `strategy_results.jsonl` but NOT to the `trades` table.
**Impact:** Attribution works (JSONL); DB trade ledger is incomplete for partials.
**Fix approach:** Add a separate `update_trade_partial` repo call inside `fill_partial_take_profit_position`, or relax the `close_db_position` guard to write a partial-exit row without closing the position.
**Files:** `trading_bot/runtime/position_exit.py:141-183`, `trading_bot/db/repositories/trades.py`.
**Effort:** S-M.

### 12. [READY] Swarm APPROVE boost is structurally a no-op
**Problem:** The cap at `orchestrator.py:1028-1030` reverts every APPROVE/sentiment boost back to `risk_approved_size`. Only REJECT and bearish sentiment persist.
**Impact:** Swarm sentiment boost field is logged but has zero sizing effect on the upside.
**Fix approach:** Either remove the boost code path (and the misleading `swarm_sentiment_size_multiplier` log field) or reframe AGENTS.md to say swarm sentiment is downside-only. Removing dead boosts is cleaner.
**Files:** `trading_bot/runtime/orchestrator.py:1015-1030`.
**Effort:** S.

---

## P2 — Dead code / stale docs / cosmetic

### 13. [DONE] Dead advisory outputs
**Problem:** `state/advisory_learner/recommendations.{main_midcap,cheap_stocks}.json` are written (`learner.py:116-117`) but never read by production code (only tests).
**Fix:** Delete the writes or wire them in. Delete is safer.
**Files:** `trading_bot/advisory/learner.py:43-44, 116-117`.

### 14. [DONE] Dead functions
**Problem:** `_portfolio_state_after_sell` (`cli/app.py:1749`), `update_burn_in_symbols` / `create_watchlist_from_breakouts` (`dynamic_watchlist.py:381, 414`), `_short_swarm_decision` (`orchestrator.py:2625`).
**Fix:** Delete.

### 15. [DONE] Stale comments / docstrings
**Problem:** (a) `auto-burn-in.sh:371` says "V3 + RL + Swarm" but RL is disabled; (b) `cli/app.py:791` exit-priority comment omits `time_exit`; (c) `continuous_loop.py:397, 421` duplicate "priority 5"; (d) `SwarmSettings` docstring claims size-modifier only in `parallel` mode but code applies whenever `swarm.enabled`.
**Fix:** Update all four.

### 16. [READY] `supermodel._decision_from_layers` defaults to `block`
**Problem:** `supermodel.py:165` — a single-source BUY with lean supporting evidence is blocked rather than treated as neutral.
**Fix:** Change default to `"no_signal"` (or expose as a config switch). Needs a quick design check — was the conservatism intentional?

### 17. [DONE] `init_db` ALTER TABLE migrations swallow all exceptions
**Problem:** `db/session.py:51-52` `except Exception: pass` masks migration failures beyond "column exists."
**Fix:** Catch `OperationalError` only (SQLite's "duplicate column") and re-raise other errors.

### 18. [WATCH] AGENTS.md implies a `record_closed_trade` ledger method
**Problem:** No such method exists; closed-trade lifecycle is split across `record_fill` + `update_trade_exit`.
**Fix:** Document the intended split in AGENTS.md or add a convenience method.

---

## Recommended order of execution

1. (done) #1 db/session.py — already fixed.
2. #3 PF confidence gate — needs a design decision first (halt vs advisory). Decide the contract, then either wire `halt_trading` or correct AGENTS.md.
3. #6 portfolio heat in loop — small, high-value, low-risk.
4. #8 `min_stop_distance_pct` default — small, prevents silent disable of a documented rule.
5. #2 dual-write transactional coupling — medium effort but the highest-consistency payoff. Do after #3 so the PF gate can rely on consistent data.
6. #4 unify manage-positions — medium effort; do as a focused refactor with the existing tests as the safety net.
7. #5 counter-thesis validate + cache — pairs well with #4.
8. P1 remainder (#7, #10, #11, #12) — batch these; each is small and isolated.
9. P2 cleanup — opportunistic, one PR.

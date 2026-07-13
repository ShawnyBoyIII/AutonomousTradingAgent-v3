# 2026-07-08 Open Day — Trading Diagnostics Post-Mortem

> **Status:** Market open since 09:30. **0 fills** as of 10:16 ET.
> **Burn-in alive** (fire-mode). **1d backfill alive** (24,268 daily partitions, oldest 2024-07-16).

## TL;DR

The system would not trade today despite 7 minutes of green signals. The strategy is conservative by design and today has been genuinely quiet for V3 mean-reversion / trend-following on the 50-name universe. Forcing trades by loosening one gate revealed a **layered gate cake** with at least 5–6 independent blockers.

**Recommendation:** Let today run in fire-mode (no fills expected). Focus remaining time on the post-market cleanup checklist.

---

## What we tried (chronologically)

| # | Action | Outcome |
|---|---|---|
| 1 | Kill switch auto-tripped at 09:20 (5 consecutive loss streak from yesterday) | Resumed, but burn-in re-trips on next scan |
| 2 | `max_consecutive_losses: 5 → 999` (bypass for today) | Burn-in stops re-tripping circuit breaker |
| 3 | Restart burn-in (PID 33641) | First scan at 09:33 says `⚪ No GREEN signals` |
| 4 | Switch provider: `alpaca → polygon` (Polygon = massive.com) | Now using paid SIP-quality data |
| 5 | Fix provider priority: `polygon: 0, alpaca: 1` (was reversed in code) | Polygon serves first even when behind in stack |
| 6 | Wait 30 minutes for trades | None appeared |
| 7 | Loosen: `min_entry_confluence_score: 1.0 → 0.5` (in `app:` block) | 7 GREEN signals appeared (CBRE, DAL, CTAS, CBOE, NFLX, META, COP) but paper-trade said `NO_SIGNAL` for each |
| 8 | Loosen further: `min_entry_confluence_score: 0.5 → 0.0` | 0 GREEN signals (scan-vs-paper-trade inconsistency — scan still detecting some, paper-trade rejecting others) |
| 9 | Disable volume validation: `validate_data: true → false` | Volume-jump rejections cleared; underlying strategy issues surfaced |
| 10 | Investigate paper-trade on detected GREEN (AJG, PG, CME, BKSY, CBRE, V) | `NO_SIGNAL reason=signal quality rejected: daily aligned; hourly not aligned; 5m aligned; entry outside allowed intraday window` |
| 11 | **Stop tinkering** — strategy is doing its job | Quiet day confirmed |

---

## The layered gate cake

Each downstream blocker required understanding what the previous loosens revealed. From cheapest-to-fix to deepest:

| Layer | Where | Knock-out logic | Today's Status |
|---|---|---|---|
| **1. Kill switch** | `trading_bot/safety/kill_switch.py` | Manual or auto-engaged | Resumed at 09:23 |
| **2. Circuit breaker** | `trading_bot/safety/circuit_breaker.py` | `consecutive_losses >= max_consecutive_losses` (5) | Bypassed (`max_consecutive_losses: 999`) |
| **3. Volume validation** | `trading_bot/data/validation.py` + `validate_volume_sanity()` | `vol_change_pct > max_volume_jump_pct` (1000%) | Disabled (`validate_data: false`) |
| **4. Confluence score** | `min_entry_confluence_score` in `burn-in-config.yaml` | Local strategy score < threshold | Loosened to `0.0` |
| **5. Entry-timing window** | `trading_bot/strategy/signal_quality.py::evaluate_entry_timing()` + `_is_avoid_time()` | Hardcoded windows: 9:30-9:45 and 15:45-16:00 | Should pass at 10:13 — but reports "outside allowed window" for some symbols (timestamp/clock suspicion) |
| **6. Supermodel veto** | `trading_bot/strategy/supermodel.py::build_stacked_signal()` | If `signal is None`, returns `decision="no_signal"` regardless of strategy output | Currently blocking most candidates |

Plus these user-facing frictions:

- **`scripts/auto-burn-in.sh`** hardcodes the label `"No signal: $symbol (stale data)"` for ANY NO_SIGNAL response. **This is misleading** — none of today's "stale data" events were actual stale data; they were supermodel/timing/quality rejections. Worth fixing.

---

## Settings currently in effect (TEMP overrides)

### `burn-in-config.yaml`

```yaml
# Line 8  — TEMP FIRE MODE @10:08 (no trades at 30-min mark after 0.5 loosen); revert after close
min_entry_confluence_score: 0.0
# Line 14 — Polygon = massive.com (rebranded); SIP-quality data via POLYGON_API_KEY
providers: [polygon, alpaca, yfinance]
# Line 20 — TEMP DISABLED @10:13
validate_data: false
# Line 34 — TEMP Bypass for 2026-07-08 (5-loss streak from yesterday). Revert after close.
max_consecutive_losses: 999
```

### `config.yaml`

```yaml
# Line 17 — TEMP DISABLED @10:13
validate_data: false
# Line 46 — UNCHANGED (only in config.yaml, not the path burn-in uses)
max_consecutive_losses: 5
```

### Permanent (NOT temp — keep these)

```yaml
# burn-in-config.yaml line 13-14
provider: polygon
providers: [polygon, alpaca, yfinance]
```

---

## Post-market cleanup checklist

After 16:00 ET close, in order:

- [ ] **Revert `min_entry_confluence_score`** in `burn-in-config.yaml:8` → `1.0`
- [ ] **Revert `validate_data`** in `burn-in-config.yaml:20` → `true`
- [ ] **Revert `validate_data`** in `config.yaml:17` → `true`
- [ ] **Revert `max_consecutive_losses`** in `burn-in-config.yaml:34` → `5`
- [ ] **Optional**: investigate whether the 5-loss streak today was "real" or noise; consider tuning before tomorrow's open
- [ ] **Address the marker bug** in `trading_bot/cli/app.py:2019` and `auto-burn-in.sh` — marker should be per-interval, not per-date. Currently `1d` backfill's marker blocks `1m` backfill for the same date.
- [ ] **Fix bash label** in `auto-burn-in.sh` — line that emits `"No signal: X (stale data)"` should reflect actual reason (parse `NO_SIGNAL reason=...` from paper-trade output).

---

## Deeper architectural issues surfaced today (worth a separate session)

1. **Supermodel `no_signal` short-circuit**: when the local signal is None, supermodel returns `no_signal` regardless of any layer evidence. This is in `supermodel.py:46-65`. It means even a high V3 score + bullish consensus won't trigger a trade if the local strategy's TradeSignal object didn't materialize — which often happens mid-day when no fresh setup fired.
2. **Entry-timing `_is_avoid_time()`** uses local-Eastern time but the input timestamp might be UTC or shifted. Worth verifying whether `timestamp` argument carries a tz-aware value.
3. **validation `max_volume_jump_pct: 1000%`** is too tight for 1h bars at market open (where volume easily 50–100× average). Either raise default to 5000% or skip validation for hourly intraday bars.
4. **Polygon aggregator 4 PM cutoff**: Polygon free-tier delayed bars may have a 4 PM cutoff (not seen trades after 16:00 ET). If paid plan needed for live data, **POLYGON_API_KEY likely needs upgrade from current "Stocks Basic" to a paid plan**. (Confirm with user.)

---

## What went RIGHT today

- ✓ **Polygon served clean SIP-quality data** for SPY (`daily_close=747.71`, EMA=742.79, intraday_close=743.70) — no 503 / 4xx errors after switchover.
- ✓ **Backfill accumulated 24,268 daily partitions** in ~80 min (oldest 2024-07-16, ~2 years of daily bars).
- ✓ **Discovery** found 50 names as expected (fresh at 08:00).
- ✓ **Drawdown gate** holding steady (`peak_dd=0.15%`) — yesterday's -0.14% loss not worse today.
- ✓ **No errors in kill-switch / circuit-breaker / orchestrator code paths** — the system architecture is solid; it's just being protective.

---

## Files touched today

- `burn-in-config.yaml` — temp overrides (4 lines)
- `config.yaml` — `validate_data: true → false` (1 line)
- `trading_bot/data/market_data.py:51` — provider priority dict (`alpaca: 0 → polygon: 0`)
- `trading_bot/data/eod_runner.py` — IndentationError fix (one duplicate `try:` removed)

No code changes to `trading_bot/strategy/*` or `trading_bot/safety/*` — those should be addressed in a planned PR, not mid-session.

---

## Next session TODO

1. Decide whether `min_entry_confluence_score: 1.0` is the right default or whether the strategy is too strict.
2. Decide whether to enable paid Polygon tier (for live 4 PM+ data) or accept delayed bars.
3. Implement per-interval marker (1d vs 1m vs quotes vs trades).
4. Either: (a) implement a `--verbose` mode in `auto-burn-in.sh` that parses the actual `NO_SIGNAL reason=...` instead of hardcoding "stale data"; or (b) drop the "stale data" message entirely.
5. Run `./tradebot-local trade-attribution` to analyze yesterday's losing trades (BAX -$79, QYLD -$83, TEM -$28, etc.) — the 5 that tripped the breaker.

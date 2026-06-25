# V3 Roadmap: Robinhood Shadow Integration

## Goal
Read-only integration with Robinhood for account sync, market data parity, and shadow trading. **No live order placement.** This is the bridge between paper trading (V2) and live trading (V4).

## Philosophy
**Safety first, always.** V3 proves the integration works without risking capital. Every feature is designed to prevent accidental live trades.

---

## Phase V3.1: Infrastructure & Safety Foundation

### Task 1: Broker Abstraction Layer
**Why:** Decouple Robinhood-specific code from core logic so we can swap brokers later.

**Implementation:**
- Create `trading_bot/brokers/` package
- Define `BrokerAdapter` abstract base class
- Methods: `get_account()`, `get_positions()`, `get_orders()`, `preview_order()`, `submit_order()` (raises if not implemented)
- Refactor existing `PaperBroker` to implement interface

**Safety:** Ensure `submit_order()` has explicit `mode` parameter with no default.

### Task 2: Configuration & Credentials
**Why:** Secure credential storage outside git.

**Implementation:**
- Add `RobinhoodSettings` to config with:
  - `enabled: false` (default)
  - `username: str` (can be env var reference)
  - `password: str` (env var only, never hardcoded)
  - `mfa_secret: str` (env var, for 2FA)
  - `device_token: str | None`
- Create `.env.example` file with placeholder values
- Update `.gitignore` to exclude `.env`
- Add config validation that refuses to start if creds in git

**Safety:** App refuses to start Robinhood mode if credentials detected in config file vs env vars.

### Task 3: Authentication Module
**Why:** Secure, refreshable connection to Robinhood.

**Implementation:**
- Create `trading_bot/brokers/robinhood/auth.py`
- Handle login with 2FA support
- Token refresh logic
- Session management with timeouts
- Log authentication events (success/failure) to audit trail

**Safety:** 
- Max 3 login attempts before lockout
- All auth events logged with timestamp
- No token persistence in logs

---

## Phase V3.2: Read-Only Data Sync

### Task 4: Account Information Sync
**Why:** Verify local portfolio matches broker reality.

**Implementation:**
- `get_account()` → returns buying_power, equity, cash
- New CLI: `sync-account`
- Compare local SQLite vs Robinhood
- Alert on discrepancies > 1%

**Safety:** Read-only. No write operations.

### Task 5: Position Sync
**Why:** Ensure we know what positions actually exist.

**Implementation:**
- `get_positions()` → list of broker positions
- New CLI: `sync-positions --dry-run` (shows diff only)
- Reconcile local positions vs broker
- Handle partial fills, corporate actions

**Safety:** 
- `--dry-run` by default
- Separate `--apply` flag required to update local state
- Warn if position quantities don't match

### Task 6: Order History Sync
**Why:** Reconcile fills that happened outside our system.

**Implementation:**
- `get_orders()` → recent order history
- New CLI: `sync-orders`
- Match broker orders to local order log
- Identify orphaned orders (in broker, not in our DB)

**Safety:**
- Never modify broker orders
- Flag discrepancies for manual review
- Log all sync operations

---

## Phase V3.3: Market Data Parity

### Task 7: Real-Time Quote Comparison
**Why:** Ensure our data feed matches broker's view of market.

**Implementation:**
- New CLI: `market-parity --symbols SPY,AAPL`
- Fetches quotes from both yfinance and Robinhood
- Compares bid/ask/last price
- Alert if deviation > 0.5%

**Safety:** Read-only comparison. No trades.

### Task 8: Tradability Checks
**Why:** Not all symbols trade on all brokers.

**Implementation:**
- `is_tradable(symbol)` → bool
- Check if symbol exists, is tradeable, not halted
- New CLI: `check-symbol AAPL`
- Integrate into scan to warn about non-tradable symbols

**Safety:** Prevents attempting to trade delisted/suspended stocks.

---

## Phase V3.4: Shadow Trading Mode

### Task 9: Order Preview System
**Why:** See exactly what the broker would do before committing.

**Implementation:**
- `preview_order(order)` → returns estimated fill price, fees, etc.
- New CLI: `preview-trade --symbol AAPL --qty 10`
- Shows:
  - Estimated fill price
  - Commission/fees
  - Total cost
  - Impact on buying power
  - Risk metrics

**Safety:** Explicit confirmation required. Preview != execution.

### Task 10: Shadow Mode
**Why:** Log "would have traded" without actually trading.

**Implementation:**
- New config: `mode: shadow`
- When shadow mode active:
  - All order logic runs
  - Preview generated
  - Logged to `shadow_trades.jsonl`
  - **No actual order submitted**
- New CLI: `shadow-report` → compares shadow trades to what paper would have done

**Safety:** 
- Mode prominently displayed in all CLI output
- Separate shadow database
- Kill switch works in shadow mode too

### Task 11: Side-by-Side Comparison
**Why:** Validate paper vs shadow vs actual market.

**Implementation:**
- Run paper and shadow simultaneously
- Compare fills:
  - Paper fill price vs shadow preview price
  - Shadow preview vs actual market price
- Weekly report: accuracy of paper simulation

**Safety:** No live money at risk.

---

## Phase V3.5: Audit & Controls

### Task 12: Comprehensive Audit Logging
**Why:** Every broker interaction must be traceable.

**Implementation:**
- New table: `audit_log` in SQLite
- Log: timestamp, action, user, result, correlation_id
- Actions logged:
  - Login attempts
  - Data syncs
  - Order previews
  - Mode changes
  - Kill switch triggers
- New CLI: `audit-log --since 2026-06-01`

**Safety:** Audit log is append-only. Never deleted, never modified.

### Task 13: Multi-Level Kill Switches
**Why:** Multiple safety nets before any live trading.

**Implementation:**
- Level 1: `kill-switch` (existing) - stops all trading
- Level 2: `robinhood-mode off` - disables broker entirely
- Level 3: `shadow-only` - allows preview but blocks submit
- Level 4: `max-live-notional` - hard cap on live position size (prep for V4)

**Safety:** All levels default to OFF. Must explicitly enable each.

### Task 14: Operator Commands
**Why:** Human oversight for all broker operations.

**New CLI Commands:**
```bash
# Account management
robinhood-status         # Show snapshot status + account summary

# Data sync
sync-account             # Read synced account snapshot
sync-positions           # Read synced positions snapshot
sync-positions --apply   # Rejected locally; operator review required

# Market data
market-parity            # Compare data feeds
check-symbol AAPL        # Verify tradability

# Trading (intent mode)
preview-trade AAPL 10    # Future intent/log workflow

# Safety
robinhood-disable       # Completely disable RH integration
```

**Safety:** Every command logs to audit trail. No command auto-executes live trades.

---

## Phase V3.6: Testing & Validation

### Task 15: Integration Test Suite
**Why:** Verify broker integration without real credentials.

**Implementation:**
- Mock Robinhood API responses
- Test auth flow
- Test all sync operations
- Test error handling (rate limits, downtime)
- Test kill switches

**Tests:** 20+ integration tests, no real API calls.

### Task 16: Paper-to-Shadow Validation
**Why:** Ensure paper simulation matches broker reality.

**Implementation:**
- Run paper trading for 1 week
- Simultaneously run paper flow and operator-managed broker snapshots
- Compare:
  - Entry prices (paper vs shadow preview)
  - Fill times
  - Slippage estimates
- Tolerance: ±2% acceptable

**Success Criteria:** Shadow prices within 2% of actual market fills.

### Task 17: Security Audit
**Why:** Ensure no credentials leak.

**Checklist:**
- [ ] No credentials in git history
- [ ] No credentials in logs
- [ ] No credentials in error messages
- [ ] Env vars only for secrets
- [ ] Rate limiting to prevent API abuse
- [ ] Session timeout after 30 min inactivity

**Tools:** `git-secrets`, `truffleHog` scan.

---

## Phase V3.7: Documentation & Runbook

### Task 18: V3 Integration Guide
Document:
- How to set up Robinhood credentials
- How to sync broker snapshots through Codex MCP
- How to read audit logs
- What to do when alerts fire
- Emergency procedures

### Task 19: Runbook Template
Create `RUNBOOK.md`:
- Daily checklist (5 minutes)
- Weekly review (30 minutes)
- Incident response procedures
- How to escalate issues

### Task 20: ADR-002
Document architecture decisions:
- Why MCP snapshots and intent review before any live workflow
- Why read-only first
- Why audit logging is mandatory
- Kill switch design

---

## V3 Success Criteria (Before V4)

**Must Have:**
- ✅ 2+ weeks of shadow trading data
- ✅ Paper vs shadow comparison within 2%
- ✅ Zero credential leaks
- ✅ All audit logs working
- ✅ Kill switches tested monthly
- ✅ Operator comfortable with all CLI commands

**Red Lines (Do not proceed to V4):**
- ❌ Any shadow trade executed as live
- ❌ Credentials in git/logs
- ❌ Missing audit trail entries
- ❌ Operator not trained on emergency procedures

---

## V3 vs V4 Boundary

**V3 (This Phase):**
- Read-only from broker
- Shadow trading (log only)
- No real money at risk
- Human approval for everything

**V4 (Next Phase):**
- Live order placement
- Real money at risk
- Automated workflows
- Broker review before submit

**The Gap:** V3 → V4 requires explicit decision. No accidental graduation.

---

## Timeline Estimate

**V3.1-3.2 (Infrastructure):** 2-3 days
**V3.3-3.4 (Shadow Mode):** 3-4 days  
**V3.5-3.6 (Audit & Testing):** 2-3 days
**V3.7 (Documentation):** 1 day

**Total:** ~2 weeks development + 2-4 weeks shadow validation

---

**Ready to start V3?** Begin with Task 1 (Broker Abstraction) - it's the foundation for everything else.
> Historical note: sections below still mention direct Robinhood auth and shadow mode. The current supported boundary is MCP/operator-managed snapshots plus local intent logs.

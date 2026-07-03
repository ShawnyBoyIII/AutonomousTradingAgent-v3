# V2.5 Phase D: Paper Burn-In Guide

## What is Paper Burn-In?

Paper burn-in is the real-world validation phase where you run the trading bot in paper mode for 2-4 weeks to verify it behaves correctly before considering live trading (V3).

**Goal:** Confirm theory matches reality.

## Do You Need to Keep Your Laptop On 24/7?

**Short answer: No, but with caveats.**

### Option 1: Laptop Method (Recommended for Testing)
**Schedule:** Run during market hours only (9:30 AM - 4:00 PM ET weekdays)

**Pros:**
- Simple to start
- Easy to monitor
- Can pause/resume anytime

**Cons:**
- Misses pre-market/after-hours data
- Gaps if laptop sleeps

**Best for:** Initial testing, learning the system

### Option 2: 24/7 Server Method (Production-Ready)
**Schedule:** Continuous daemon with `--interval 60`

**Options:**
1. **Old laptop/desktop** - Leave plugged in, disable sleep
2. **Raspberry Pi** - $100, 5W power draw
3. **Cloud VPS** - $5-10/month (DigitalOcean, AWS, GCP)
4. **NAS/Home server** - If you already have one

**Pros:**
- Captures all market data
- Runs continuously
- Professional setup

**Cons:**
- Requires dedicated hardware
- Higher setup complexity

**Best for:** Serious paper trading, path to V3

---

## Quick Start: Fully Automated (Recommended)

### Step 1: One-Command Start

```bash
cd /Users/shawndlima/Documents/AutonomousTradingAgentcopy

# This starts FULLY AUTOMATED burn-in
# It will scan, trade GREEN signals, and manage positions automatically
sh ./scripts/auto-burn-in.sh
```

**What this does:**
1. ✅ Checks system health
2. ✅ Verifies kill switch status
3. ✅ Tests market connection
4. ✅ **Loops every 60 seconds during market hours:**
   - Scans all symbols in `state/universe.txt`
   - Automatically trades GREEN signals
   - Manages positions (stops, targets, EOD exits)
   - Skips weekends and after-hours

### Step 2: Monitor in Another Terminal

Open a new terminal window:

```bash
cd /Users/shawndlima/Documents/AutonomousTradingAgentcopy

# Daily check (run this each morning)
sh ./scripts/burn-in-monitor.sh

# Or watch live logs
tail -f logs/burn_in/decision-log.jsonl
```

### Step 3: Stop When Done

Press `Ctrl-C` in the terminal running `auto-burn-in.sh`

---

## Manual Control Alternative

If you prefer manual control (decide each trade yourself):

```bash
# 1. Scan for signals
sh ./tradebot-local scan --symbols SPY,QQQ,AAPL --why --summary

# 2. Manually trade GREEN ones
sh ./tradebot-local paper-trade --symbols AAPL

# 3. Check positions
sh ./tradebot-local portfolio

# 4. Run position manager
sh ./tradebot-local manage-positions
```

**macOS:**
```bash
# Prevent sleep while plugged in (run before starting)
caffeinate -i &

# Or use System Preferences > Energy Saver > Never sleep
```

**Windows:**
```powershell
# Run as Administrator
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
```

**Linux:**
```bash
# Disable sleep
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

---

## What to Watch During Burn-In

### Daily Checklist (5 minutes)

Run these commands each morning:

```bash
# 1. Health check
sh ./tradebot-local health

# 2. Check for alerts
sh ./tradebot-local alerts

# 3. Review previous day performance
sh ./tradebot-local performance --daily

# 4. Check current portfolio
sh ./tradebot-local portfolio
```

### What to Look For

**✅ Good Signs:**
- Win rate between 40-60%
- Profit factor > 1.0
- No "stale data" errors
- Alerts fire correctly (test them!)
- Positions exit at stops/targets as expected
- EOD exits happen at 15:55 ET

**🚨 Red Flags:**
- Win rate < 30% (strategy may be broken)
- Consistent data validation failures
- Kill switch triggered unexpectedly
- Trades not filling when expected
- Portfolio heat constantly > 3%

---

## Weekly Deep Review (30 minutes)

### Week 1: Sanity Check
```bash
# Full performance report
sh ./tradebot-local performance --days 7

# Check logs for errors
grep -i "error\|fail\|exception" logs/burn_in/decision-log.jsonl | head -20

# Verify position sizing is reasonable
# (Should be 20% max per position with default settings)
```

### Week 2-4: Trend Analysis

Track these metrics in a spreadsheet:

| Date | Trades | Wins | Win Rate | Net P&L | Max Heat | Notes |
|------|--------|------|----------|---------|----------|-------|
| Week 1 | | | | | | |
| Week 2 | | | | | | |
| Week 3 | | | | | | |
| Week 4 | | | | | | |

**Questions to answer:**
1. Is win rate stable or trending down?
2. Are average wins larger than average losses?
3. Does the strategy handle volatility well?
4. Are there periods of excessive drawdown?
5. Do the technical indicators work as expected?

---

## Testing Specific Scenarios

### Test 1: Kill Switch
```bash
# Trigger halt
sh ./tradebot-local kill-switch --halt --reason "Test emergency stop"

# Verify no new trades execute
sh ./tradebot-local scan --symbols SPY --why
# Should see: KILL_SWITCH: Trading halted

# Resume trading
sh ./tradebot-local kill-switch --resume
```

### Test 2: Data Validation
```bash
# Check logs for validation failures
grep "validation_failed" logs/burn_in/decision-log.jsonl

# Should be rare - if frequent, check data source
```

### Test 3: Circuit Breaker
```bash
# Monitor for consecutive losses
sh ./tradebot-local alerts

# Should warn if 5+ consecutive losses
```

### Test 4: EOD Exit
```bash
# Watch at 15:50-15:55 ET
# Verify positions close automatically

# Check logs
grep "FILLED reason=eod" logs/burn_in/decision-log.jsonl
```

---

## Common Issues & Solutions

### Issue: "No GREEN signals for days"

**Possible causes:**
1. Market is bearish (expected - strategy is long-only)
2. Scan symbols are too narrow
3. Data validation too strict

**Solutions:**
- Expand symbol universe
- Check market regime (SPY trend)
- Review data validation logs

### Issue: "Stale data errors"

**Possible causes:**
1. Laptop sleeping during market hours
2. Network interruptions
3. Data provider issues

**Solutions:**
- Disable sleep (see Step 5)
- Check internet connection
- Verify `max_data_age_minutes` setting

### Issue: "High portfolio heat (>3%)"

**Possible causes:**
1. Stop losses not triggering
2. Positions too large
3. Volatile market

**Solutions:**
- Check manage-positions is running
- Verify ATR-based sizing is active
- Review position sizes in portfolio

### Issue: "Different results than backtest"

**Expected!** Paper trading has:
- Slippage (real fills vs theoretical)
- Fees ($1/order)
- Partial fills (not implemented yet)
- Real market impact

**Rule of thumb:** Paper P&L should be 70-90% of backtest P&L. If <50%, investigate.

---

## Success Criteria for Completing Phase D

**Minimum 2 weeks, ideally 4 weeks of data.**

**Must Have:**
- ✅ 20+ trades minimum
- ✅ Win rate > 40%
- ✅ Profit factor > 1.0
- ✅ No critical alerts
- ✅ Kill switch tested and working
- ✅ EOD exits working
- ✅ Data validation < 5% failure rate

**Nice to Have:**
- ✅ Sharpe ratio > 1.0
- ✅ Max drawdown < 10%
- ✅ Stable performance across market conditions
- ✅ No manual interventions required

**Red Lines (Stop and Fix):**
- ❌ Win rate < 30%
- ❌ Profit factor < 0.8
- ❌ Consistent data errors
- ❌ Kill switch doesn't work
- ❌ Positions not exiting at stops

---

## Moving to V3 (Live Trading Prep)

**Only proceed if:**
1. Phase D success criteria met
2. You're comfortable with the system
3. You understand the risks
4. You have a separate live trading checklist

**V3 is Robinhood integration** - requires:
- Broker API credentials
- Read-only account sync
- Shadow mode testing
- Manual approval workflows

---

## Quick Reference Card

```bash
# Start burn-in
sh ./tradebot-local run-manager --interval 60

# Daily checks
sh ./tradebot-local health
sh ./tradebot-local alerts
sh ./tradebot-local performance --daily

# Emergency stop
sh ./tradebot-local kill-switch --halt --reason "Emergency"

# Resume
sh ./tradebot-local kill-switch --resume

# View logs
tail -f logs/burn_in/decision-log.jsonl

# Check status
curl -s localhost:8080/health  # If you set up HTTP endpoint
```

---

## Customizing Automated Trading

### Change Scan Frequency

Edit `scripts/auto-burn-in.sh`:
```bash
# Change this line (default: 60 seconds)
sleep 60

# To scan every 5 minutes:
sleep 300

# To scan every 15 minutes:
sleep 900
```

### Change Symbol Universe

Edit `state/universe.txt`:
```bash
# Remove symbols you don't want
# Add new symbols (one per line)

# Example: Focus on just ETFs
SPY
QQQ
IWM
VTI

# Or just tech
AAPL
MSFT
NVDA
AMD
```

### Add Filters

Edit `scripts/auto-burn-in.sh` to filter trades:

```bash
# Only trade if confidence > 0.85
if echo "$scan_output" | grep "conf=0.9" | grep -q "GREEN"; then
    # ... trade
fi

# Only trade if volume ratio > 1.5
if echo "$scan_output" | grep "volume_ratio=1.5" | grep -q "GREEN"; then
    # ... trade
fi
```

### Limit Daily Trades

Already built-in via config:
```yaml
risk:
  max_daily_orders: 3  # Stops after 3 orders per day
```

---

## FAQ

**Q: What if I miss a day?**
A: That's fine! The system handles gaps. Just resume the next day.

**Q: Can I run multiple instances?**
A: No - use separate database files to avoid conflicts.

**Q: What happens if my laptop dies?**
A: State is saved in SQLite. Restart and resume.

**Q: Should I watch it constantly?**
A: No - check once daily is enough. Set up alerts for critical issues.

**Q: Can I trade manually while burn-in runs?**
A: Yes, but document it separately. Don't mix manual and auto trades in analysis.

**Q: What if I see a bug?**
A: Stop the daemon, fix the code, commit, restart. Document the interruption.

---

## Support

If issues arise during burn-in:
1. Check `logs/burn_in/decision-log.jsonl`
2. Run `sh ./tradebot-local health`
3. Review this guide's "Common Issues" section
4. Check ADRs in `docs/adr/` for design decisions

---

**Remember:** Paper burn-in is about building confidence, not profit. The goal is to prove the system works reliably before risking real money.

Good luck! 🚀

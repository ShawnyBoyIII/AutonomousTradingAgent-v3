#!/bin/bash
# V3 FULLY AUTOMATED Burn-In with Dynamic Watchlist + Swarm Overlay
# This runs discover → scan → trade → manage loop continuously
#
# Scan interval: 300 seconds (5 minutes) during market hours
# Discovery: HYBRID approach
#   - Daily at market open (9:30 AM ET) - ALWAYS
#   - Mid-day (12:00-1:00 PM ET) - ONLY if watchlist < 5 symbols
# Log rotation: Automatic when decision-log.jsonl exceeds 10MB

set -e

echo "=========================================="
echo "FULLY AUTOMATED Paper Burn-In (V3 + Swarm)"
echo "Date: $(date)"
echo "Dynamic Watchlist: ENABLED"
echo "=========================================="
echo ""

cd /Users/shawndlima/Documents/AutonomousTradingAgentcopy

# Configuration
CONFIG_FILE="burn-in-config.yaml"
UNIVERSE_FILE="state/universe.txt"
WATCHLIST_FILE="state/watchlist.txt"
LOG_DIR="logs"
DB_PATH="state/burn_in.db"
LAST_DISCOVER_FILE=".last_discover_date"

# Ensure setup exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Creating burn-in configuration..."
    cat > "$CONFIG_FILE" << 'EOF'
app:
  timezone: "America/New_York"
  state_db_path: "state/burn_in.db"
  log_dir: "logs/burn_in"
  portfolio_summary_path: "state/burn_in/portfolio_summary.json"
  scan_results_path: "state/burn_in/scan_results.json"

market_data:
  provider: "yfinance"
  daily_period: "1y"
  intraday_period: "5d"
  intraday_interval: "5m"
  max_data_age_hours: 72
  max_data_age_minutes: 30
  validate_data: true

risk:
  max_risk_per_trade_pct: 0.01
  max_daily_risk_pct: 0.03
  max_daily_orders: 3
  max_ticker_allocation_pct: 0.20
  min_reward_risk_ratio: 2.0
  use_atr_sizing: true
  max_portfolio_heat_pct: 0.03

session:
  eod_enabled: true
  eod_minutes_before_close: 5

paper:
  fee_per_order: 1.0
  slippage_bps: 0

strategy:
  use_v3_signals: true
  risk_tolerance: "medium"
  min_confidence: "medium"

counter_thesis:
  enabled: true
  block_on_severity: "high"
  aggregate_block_threshold: 0.6
  exit_on_block: true

swarm:
  enabled: true
  preset: "investment_committee"
  max_workers: 4
EOF
fi

# Create default symbols file if missing (will be overwritten by discover)
if [ ! -f "$UNIVERSE_FILE" ]; then
    echo "Creating initial symbol universe..."
    cat > "$UNIVERSE_FILE" << 'EOF'
SPY
QQQ
AAPL
MSFT
NVDA
EOF
fi

# Create log directory
mkdir -p "$LOG_DIR"

# Function to rotate logs if they exceed 10MB
rotate_logs() {
    local max_size=10485760  # 10MB in bytes
    local decision_log="$LOG_DIR/decision-log.jsonl"
    local strategy_log="$LOG_DIR/strategy_results.jsonl"
    
    for log_file in "$decision_log" "$strategy_log"; do
        if [ -f "$log_file" ]; then
            local size=$(stat -f%z "$log_file" 2>/dev/null || stat -c%s "$log_file" 2>/dev/null || echo 0)
            if [ "$size" -gt "$max_size" ]; then
                local timestamp=$(date '+%Y%m%d_%H%M%S')
                local backup="${log_file}.${timestamp}"
                mv "$log_file" "$backup"
                gzip "$backup" 2>/dev/null || true
                echo "[$(date '+%H:%M:%S')] 🔄 Rotated $log_file -> ${backup}.gz"
            fi
        fi
    done
}

# Function to run discovery
run_discovery() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local trigger_reason="$1"  # "daily" or "midday"
    
    echo "[$timestamp] 🔍 Running discovery ($trigger_reason refresh)..."
    
    # Preserve manually added watchlist symbols
    local watchlist_symbols=()
    if [ -f "$WATCHLIST_FILE" ]; then
        while IFS= read -r line; do
            line=$(echo "$line" | tr -d '[:space:]')
            if [ -n "$line" ] && [[ ! "$line" =~ ^# ]]; then
                watchlist_symbols+=("$line")
            fi
        done < "$WATCHLIST_FILE"
    fi
    
    # Run discover with export
    local discover_output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" discover --mode breakout --max 50 --export 2>&1)
    
    if echo "$discover_output" | grep -q "Exported"; then
        local count=$(echo "$discover_output" | grep "Exported" | sed 's/.*Exported \([0-9]*\).*/\1/')
        echo "[$timestamp] ✅ Discovered $count symbols"
        
        # Show top candidates
        local symbols=$(echo "$discover_output" | grep "^  [A-Z]" | head -5)
        if [ -n "$symbols" ]; then
            echo "[$timestamp] Top candidates:"
            echo "$symbols" | while read line; do
                echo "[$timestamp]   $line"
            done
        fi
        
        # Merge discovered symbols with watchlist symbols (preserve manual additions)
        local all_symbols=()
        while IFS= read -r line; do
            line=$(echo "$line" | tr -d '[:space:]')
            if [ -n "$line" ] && [[ ! "$line" =~ ^# ]]; then
                all_symbols+=("$line")
            fi
        done < "$UNIVERSE_FILE"
        
        # Add watchlist symbols if not already present
        for ws in "${watchlist_symbols[@]}"; do
            local found=0
            for existing in "${all_symbols[@]}"; do
                if [ "$existing" = "$ws" ]; then
                    found=1
                    break
                fi
            done
            if [ $found -eq 0 ]; then
                all_symbols+=("$ws")
            fi
        done
        
        # Update universe file with merged symbols
        printf "%s\n" "${all_symbols[@]}" > "$UNIVERSE_FILE"
        
        # Log discovery event
        echo "{\"event\":\"discovery\",\"timestamp\":\"$timestamp\",\"trigger\":\"$trigger_reason\",\"count\":$count,\"watchlist_preserved\":${#watchlist_symbols[@]}}" >> "$LOG_DIR/discovery.log"
    else
        echo "[$timestamp] ⚠️  Discovery returned no symbols, preserving existing list"
        # Log failed discovery
        echo "{\"event\":\"discovery\",\"timestamp\":\"$timestamp\",\"trigger\":\"$trigger_reason\",\"count\":0,\"status\":\"failed\"}" >> "$LOG_DIR/discovery.log"
    fi
    
    # Update last discover date
    date +%Y-%m-%d > "$LAST_DISCOVER_FILE"
}

# Check if watchlist is running low on symbols
watchlist_is_low() {
    local threshold=${1:-5}  # Default threshold: 5 symbols
    if [ ! -f "$UNIVERSE_FILE" ]; then
        return 0  # No file = definitely low
    fi
    local count=$(wc -l < "$UNIVERSE_FILE" | tr -d ' ')
    if [ "$count" -lt "$threshold" ]; then
        echo "[$(date '+%H:%M:%S')] 📉 Watchlist low: $count symbols (threshold: $threshold)"
        return 0  # Yes, it's low
    fi
    return 1  # No, we have enough
}

# Check if it's mid-day (between 12:00 PM and 1:00 PM ET)
is_midday() {
    local current_hour=$(date +%H)
    local current_min=$(date +%M)
    local current_time=$((10#$current_hour * 60 + 10#$current_min))
    local midday_start=$((12 * 60))      # 12:00 PM
    local midday_end=$((13 * 60))        # 1:00 PM
    
    if [ "$current_time" -ge "$midday_start" ] && [ "$current_time" -lt "$midday_end" ]; then
        return 0  # Yes, it's midday
    fi
    return 1  # No
}

# Hybrid discovery logic:
# 1. ALWAYS run at market open (first cycle of day)
# 2. Run mid-day (12:00-1:00 PM ET) ONLY if watchlist < 5 symbols
# 3. Otherwise skip
should_discover() {
    if [ ! -f "$LAST_DISCOVER_FILE" ]; then
        echo "[$(date '+%H:%M:%S')] 🔍 Discovery: first run (no history)"
        return 0  # Never run before
    fi
    
    local last_date=$(cat "$LAST_DISCOVER_FILE")
    local today=$(date +%Y-%m-%d)
    
    # Check if new day - always discover at market open
    if [ "$last_date" != "$today" ]; then
        echo "[$(date '+%H:%M:%S')] 🔍 Discovery: new day ($today)"
        return 0  # New day
    fi
    
    # Already discovered today - check if we should run mid-day refresh
    if is_midday; then
        if watchlist_is_low 5; then
            echo "[$(date '+%H:%M:%S')] 🔍 Discovery: mid-day refresh (watchlist low)"
            return 0  # Mid-day refresh needed
        fi
    fi
    
    return 1  # Skip discovery
}

# Sleep until next market open (9:30 AM ET next weekday)
sleep_until_market_open() {
    local now_epoch=$(date +%s)
    local current_dow=$(date +%u)  # 1=Monday, 7=Sunday
    local current_hour=$(date +%H)
    local current_min=$(date +%M)
    local current_time=$((10#$current_hour * 60 + 10#$current_min))
    local market_open=$((9 * 60 + 30))
    
    local target_epoch
    if [ "$current_dow" -gt 5 ]; then
        # Weekend: sleep until Monday 9:30 AM
        local days_until_monday=$((8 - current_dow))
        target_epoch=$((now_epoch + days_until_monday * 86400 - current_time * 60 + market_open * 60))
    elif [ "$current_time" -ge "$market_open" ]; then
        # After market open today: sleep until tomorrow 9:30 AM (or Monday if Friday)
        if [ "$current_dow" -eq 5 ]; then
            # Friday after hours: sleep until Monday 9:30 AM
            target_epoch=$((now_epoch + 3 * 86400 - current_time * 60 + market_open * 60))
        else
            target_epoch=$((now_epoch + 86400 - current_time * 60 + market_open * 60))
        fi
    else
        # Pre-market: sleep until 9:30 AM today
        target_epoch=$((now_epoch + (market_open - current_time) * 60))
    fi
    
    local sleep_seconds=$((target_epoch - now_epoch))
    if [ "$sleep_seconds" -gt 0 ]; then
        local wake_time=$(date -r "$target_epoch" '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || date -d "@$target_epoch" '+%Y-%m-%d %H:%M:%S %Z')
        echo "[$timestamp] Sleeping until market open: $wake_time ($sleep_seconds sec)"
        sleep "$sleep_seconds"
    fi
}

# Load symbols function - reads from Python-configured paths (universe + watchlist)
load_symbols() {
    local all_symbols=()
    
    # Read universe file (ranked symbols from build-universe)
    if [ -f "$UNIVERSE_FILE" ]; then
        while IFS= read -r line; do
            line=$(echo "$line" | tr -d '[:space:]')
            if [ -n "$line" ] && [[ ! "$line" =~ ^# ]]; then
                all_symbols+=("$line")
            fi
        done < "$UNIVERSE_FILE"
    fi
    
    # Read watchlist file (manually added symbols)
    if [ -f "$WATCHLIST_FILE" ]; then
        while IFS= read -r line; do
            line=$(echo "$line" | tr -d '[:space:]')
            if [ -n "$line" ] && [[ ! "$line" =~ ^# ]]; then
                # Add if not already in universe
                local found=0
                for existing in "${all_symbols[@]}"; do
                    if [ "$existing" = "$line" ]; then
                        found=1
                        break
                    fi
                done
                if [ $found -eq 0 ]; then
                    all_symbols+=("$line")
                fi
            fi
        done < "$WATCHLIST_FILE"
    fi
    
    # If no symbols found, use defaults
    if [ ${#all_symbols[@]} -eq 0 ]; then
        all_symbols=("SPY" "QQQ" "AAPL" "MSFT" "NVDA")
    fi
    
    # Join symbols with commas
    SYMBOLS=$(IFS=','; echo "${all_symbols[*]}")
}

echo "Configuration:"
echo "  Config: $CONFIG_FILE"
echo "  Symbols File: $UNIVERSE_FILE"
echo "  Database: $DB_PATH"
echo "  Mode: Dynamic discovery + V3 signals + Swarm overlay"
echo ""

# Pre-flight checks
echo "Pre-flight Checks:"
echo "------------------"

# Check local readiness. ponytail: burn-in should not refuse to start just
# because the existing paper ledger had a bad week; runtime commands already
# enforce kill switch and the next check below verifies market data access.
if ! sh ./tradebot-local --config-path "$CONFIG_FILE" doctor > /dev/null 2>&1; then
    echo "❌ Doctor check failed"
    exit 1
fi
echo "✅ Local app ready"

# Check kill switch
if ! sh ./tradebot-local --config-path "$CONFIG_FILE" kill-switch > /dev/null 2>&1; then
    echo "⚠️  Kill switch is ACTIVE - cannot start"
    echo "Resume with: sh ./tradebot-local kill-switch --resume"
    exit 1
fi
echo "✅ Kill switch: Trading active"

# Test scan (use any known valid symbol)
echo "Testing market connection..."
if ! sh ./tradebot-local --config-path "$CONFIG_FILE" scan --symbols SPY --summary > /dev/null 2>&1; then
    echo "❌ Market connection failed"
    exit 1
fi
echo "✅ Market data accessible"
echo ""

echo "=========================================="
echo "Starting FULLY AUTOMATED Burn-In"
echo ""
echo "This will:"
echo "  1. Discover new candidates on first cycle of each day"
echo "  2. Scan universe every 60 seconds during market hours"
echo "  3. Auto-trade GREEN signals (V3 + Swarm overlay; RL disabled in burn-in)"
echo "  4. Manage positions (stops, targets, EOD)"
echo "  5. Log everything to $LOG_DIR"
echo ""
echo "To monitor:"
echo "  New terminal: sh ./scripts/burn-in-monitor.sh"
echo "  Live log:    tail -f $LOG_DIR/decision-log.jsonl"
echo ""
echo "To stop: Press Ctrl-C"
echo "=========================================="
echo ""

# Function to run RL model comparison
run_rl_compare() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] 🤖 Running RL model comparison..."
    
    local rl_output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" rl-compare --symbols "$SYMBOLS" 2>&1)
    
    if echo "$rl_output" | grep -q "No RL models found"; then
        echo "[$timestamp] ⚪ No RL models available for comparison"
        return 0
    fi
    
    if echo "$rl_output" | grep -q "Best P&L"; then
        local best_pnl=$(echo "$rl_output" | grep "Best P&L" | sed 's/.*Best P&L: //')
        local best_wr=$(echo "$rl_output" | grep "Best Win Rate" | sed 's/.*Best Win Rate: //')
        echo "[$timestamp] 🤖 RL comparison complete - Best P&L: $best_pnl, Best Win Rate: $best_wr"
        
        # Log RL comparison result
        echo "{\"event\":\"rl_compare\",\"timestamp\":\"$timestamp\",\"best_pnl_strategy\":\"$best_pnl\",\"best_winrate_strategy\":\"$best_wr\"}" >> "$LOG_DIR/rl_comparison.log"
    else
        echo "[$timestamp] ⚠️  RL comparison had issues"
    fi
}

# Function to refresh tuning overrides from recent paper results
run_nightly_tuning() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] 🧠 Running nightly tuning..."

    local tune_output
    tune_output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" tune 2>&1)
    local status=$?

    if [ $status -ne 0 ]; then
        echo "[$timestamp] ⚠️  Nightly tuning failed"
        echo "$tune_output" >> "$LOG_DIR/tuning.log"
        return 0
    fi

    echo "$tune_output" >> "$LOG_DIR/tuning.log"
    echo "[$timestamp] ✅ Nightly tuning complete"
    return 0
}

# Function to refresh advisory learner artifacts
run_advisory_learner() {
    if [ "$ADVISORY_ENABLED" != "true" ]; then
        return 0
    fi
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] 📚 Running advisory learner..."

    local learner_output
    learner_output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" advisory-learn 2>&1)
    echo "$learner_output" >> "$LOG_DIR/advisory.log"
    return 0
}

on_shutdown() {
    if [ "$ADVISORY_ENABLED" != "true" ]; then
        return 0
    fi
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] 📝 Writing Daily report.md before shutdown..."
    sh ./tradebot-local --config-path "$CONFIG_FILE" advisory-learn --daily-report >> "$LOG_DIR/advisory.log" 2>&1 || true
}

# Function to scan and trade
scan_and_trade() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local symbol_count=$(echo "$SYMBOLS" | tr ',' '\n' | wc -l)
    echo "[$timestamp] Scanning $symbol_count symbols..."
    
    # Run scan and capture output
    local scan_output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" scan --symbols "$SYMBOLS" --why 2>&1)
    
    # Check if kill switch is active
    if echo "$scan_output" | grep -q "KILL_SWITCH"; then
        echo "[$timestamp] ⚠️  Kill switch active - skipping trade cycle"
        return 0
    fi
    
    # Extract GREEN symbols (approved with quality=GREEN)
    # Format: SYMBOL APPROVED quality=GREEN ...
    local green_symbols=$(echo "$scan_output" | grep "APPROVED.*quality=GREEN" | awk '{print $1}' | tr '\n' ',' | sed 's/,$//')
    
    if [ -n "$green_symbols" ]; then
        echo "[$timestamp] 🟢 GREEN signals detected: $green_symbols"
        echo "[$timestamp] Executing paper trades..."
        
        # Trade each GREEN symbol
        IFS=',' read -ra SYMBOL_ARRAY <<< "$green_symbols"
        for symbol in "${SYMBOL_ARRAY[@]}"; do
            # Check daily limits first
            local trade_output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" paper-trade --symbols "$symbol" 2>&1)
            
            if echo "$trade_output" | grep -q "FILLED"; then
                echo "[$timestamp] ✅ Filled: $symbol"
            elif echo "$trade_output" | grep -q "REJECTED"; then
                local reason=$(echo "$trade_output" | grep "REJECTED" | head -1)
                echo "[$timestamp] ❌ Rejected: $symbol - $reason"
            elif echo "$trade_output" | grep -q "NO_SIGNAL"; then
                echo "[$timestamp] ⚪ No signal: $symbol (stale data)"
            fi
        done
    else
        echo "[$timestamp] ⚪ No GREEN signals"
    fi
    
    # Always run manage-positions to check stops/targets/EOD
    echo "[$timestamp] Managing positions..."
    local manage_output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" manage-positions 2>&1)
    
    # Log summary
    if echo "$manage_output" | grep -q "actions=0"; then
        echo "[$timestamp] ✓ No position actions needed"
    else
        # Extract and display actions
        local summary=$(echo "$manage_output" | grep "positions=" | head -1)
        echo "[$timestamp] 📊 $summary"
        
        # Show any fills
        local fills=$(echo "$manage_output" | grep "FILLED")
        if [ -n "$fills" ]; then
            echo "[$timestamp] 💰 Fills:"
            echo "$fills" | while read line; do
                echo "[$timestamp]    $line"
            done
        fi
    fi
}

# Function to check confidence gates before trading
check_confidence_gates() {
    local db_path="$1"
    local min_trades=${2:-10}
    local min_net_pnl=${3:-500}
    
    if [ ! -f "$db_path" ]; then
        echo "[$(date '+%H:%M:%S')] ⚠️  Confidence gates: no database yet, skipping gates"
        return 0
    fi
    
    local result=$(.venv/bin/python -c "
import sqlite3
import json
import sys

db_path = '$db_path'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Get total trades
cursor.execute('SELECT COUNT(*) FROM orders')
total_trades = cursor.fetchone()[0]

# Get realized PnL from latest portfolio_state
cursor.execute('SELECT payload FROM portfolio_state ORDER BY id DESC LIMIT 1')
row = cursor.fetchone()
if row:
    state = json.loads(row[0])
    realized_pnl = state.get('realized_pnl', 0)
else:
    realized_pnl = 0

conn.close()

# Check gates
min_trades = $min_trades
min_net_pnl = $min_net_pnl

gates_passed = True
issues = []

if total_trades < min_trades:
    gates_passed = False
    issues.append(f'trades={total_trades}<{min_trades}')

if realized_pnl < min_net_pnl:
    gates_passed = False
    issues.append(f'pnl={realized_pnl:.0f}<{min_net_pnl}')

if gates_passed:
    print('GATES_PASSED')
else:
    print(f'GATES_FAILED: {\", \".join(issues)}')
" 2>/dev/null)
    
    if echo "$result" | grep -q "GATES_PASSED"; then
        echo "[$(date '+%H:%M:%S')] ✅ Confidence gates passed"
        return 0
    elif echo "$result" | grep -q "GATES_FAILED"; then
        echo "[$(date '+%H:%M:%S')] ⚠️  Confidence gates NOT met: $result"
        return 1
    else
        echo "[$(date '+%H:%M:%S')] ⚠️  Could not evaluate confidence gates"
        return 0
    fi
}

# Function to check max drawdown and halt if exceeded
check_max_drawdown() {
    local db_path="$1"
    local max_drawdown_pct=${2:-10}  # Default: 10% max drawdown
    
    if [ ! -f "$db_path" ]; then
        echo "[$(date '+%H:%M:%S')] ⚠️  Max drawdown check: no database yet, skipping"
        return 0
    fi
    
    local result=$(.venv/bin/python -c "
import sqlite3
import json

db_path = '$db_path'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Get equity history
cursor.execute('SELECT equity FROM equity_history ORDER BY rowid ASC')
equities = [r[0] for r in cursor.fetchall() if r[0] is not None]

if len(equities) < 2:
    print('DRAWDOWN_OK:insufficient_data')
    conn.close()
    exit(0)

# Calculate max drawdown from peak
peak = equities[0]
max_dd = 0.0
current_dd = 0.0

for eq in equities:
    if eq > peak:
        peak = eq
    dd = (peak - eq) / peak * 100 if peak > 0 else 0
    if dd > max_dd:
        max_dd = dd

# Get current equity
cursor.execute('SELECT payload FROM portfolio_state ORDER BY id DESC LIMIT 1')
row = cursor.fetchone()
if row:
    state = json.loads(row[0])
    current_equity = state.get('equity', 0)
else:
    current_equity = equities[-1] if equities else 0

# Get starting equity
starting_equity = equities[0] if equities else current_equity

# Calculate total return
total_return = (current_equity - starting_equity) / starting_equity * 100 if starting_equity > 0 else 0

conn.close()

max_dd_limit = $max_drawdown_pct

if max_dd >= max_dd_limit:
    print(f'DRAWDOWN_HALT:peak_dd={max_dd:.2f}%>={max_dd_limit}%,current_equity={current_equity:.2f},starting_equity={starting_equity:.2f}')
elif max_dd >= max_dd_limit * 0.8:
    print(f'DRAWDOWN_WARNING:peak_dd={max_dd:.2f}% approaching {max_dd_limit}%,current_equity={current_equity:.2f}')
else:
    print(f'DRAWDOWN_OK:peak_dd={max_dd:.2f}%,current_equity={current_equity:.2f},total_return={total_return:.2f}%')
" 2>/dev/null)
    
    if echo "$result" | grep -q "DRAWDOWN_HALT"; then
        echo "[$(date '+%H:%M:%S')] 🚨 MAX DRAWDOWN HALT: $result"
        echo "[$(date '+%H:%M:%S')] 🚨 Halting burn-in - drawdown exceeded ${max_drawdown_pct}%"
        echo "[$(date '+%H:%M:%S')] Review: tail -f $LOG_DIR/decision-log.jsonl"
        echo "[$(date '+%H:%M:%S')] Resume manually after review: sh ./scripts/auto-burn-in.sh"
        # Log halt event
        echo "{\"event\":\"max_drawdown_halt\",\"timestamp\":\"$(date '+%Y-%m-%d %H:%M:%S')\",\"reason\":\"$result\"}" >> "$LOG_DIR/halt.log"
        exit 1
    elif echo "$result" | grep -q "DRAWDOWN_WARNING"; then
        echo "[$(date '+%H:%M:%S')] ⚠️  Drawdown warning: $result"
        # Log warning but continue
        echo "{\"event\":\"drawdown_warning\",\"timestamp\":\"$(date '+%Y-%m-%d %H:%M:%S')\",\"reason\":\"$result\"}" >> "$LOG_DIR/halt.log"
    else
        echo "[$(date '+%H:%M:%S')] ✅ Drawdown OK: $result"
    fi
    
    return 0
}

# Main loop
echo "Starting automated loop..."
echo ""

# Track cycle count
CYCLE_COUNT=0

trap on_shutdown EXIT INT TERM

# Confidence gate thresholds (advisory: alert but don't block on PF/windows)
MIN_TRADES=50
MIN_NET_PNL=0
MAX_DRAWDOWN_PCT=10
ADVISORY_ENABLED=$(.venv/bin/python -c "from pathlib import Path; from trading_bot.config.loader import load_settings; s=load_settings(Path('$CONFIG_FILE')); print('true' if s.advisory.enabled else 'false')" 2>/dev/null || printf "false")

while true; do
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    CYCLE_COUNT=$((CYCLE_COUNT + 1))
    
    # Check if we should run discovery (hybrid: daily + conditional mid-day)
    if should_discover; then
        # Determine trigger reason for logging
        if is_midday; then
            run_discovery "midday"
        else
            run_discovery "daily"
            run_nightly_tuning
        fi
        load_symbols
    fi
    
    # Reload symbols in case file changed
    load_symbols
    
    # Check if market hours (9:30 AM - 4:00 PM ET, weekdays)
    current_hour=$(date +%H)
    current_min=$(date +%M)
    current_dow=$(date +%u)  # 1=Monday, 7=Sunday
    current_time=$((10#$current_hour * 60 + 10#$current_min))
    market_open=$((9 * 60 + 30))   # 9:30 AM
    market_close=$((16 * 60))       # 4:00 PM
    
    # If not market hours, sleep efficiently until next open
    if [ "$current_dow" -gt 5 ] || [ "$current_time" -lt "$market_open" ] || [ "$current_time" -ge "$market_close" ]; then
        sleep_until_market_open
        continue
    fi
    
    # Market is open - run cycle
    scan_and_trade
    
    # Check drawdown after each cycle
    check_max_drawdown "$DB_PATH" "$MAX_DRAWDOWN_PCT"
    
    # Show cycle summary every 10 cycles and run RL comparison
    if [ $((CYCLE_COUNT % 10)) -eq 0 ]; then
        echo "[$timestamp] 📈 Completed $CYCLE_COUNT cycles"
        
        # Check confidence gates every 10 cycles
        if ! check_confidence_gates "$DB_PATH" "$MIN_TRADES" "$MIN_NET_PNL"; then
            echo "[$timestamp] ⚠️  Confidence gates NOT met: advisory only (alert-only mode)"
            echo "[$timestamp] Trading performance below minimum thresholds"
            echo "[$timestamp] Review: tail -f $LOG_DIR/decision-log.jsonl"
            echo "{\"event\":\"confidence_gate_alert\",\"timestamp\":\"$timestamp\",\"reason\":\"gates_failed\",\"mode\":\"advisory_only\"}" >> "$LOG_DIR/halt.log"
        fi

        run_advisory_learner

        run_rl_compare
    fi
    
    echo ""
    echo "[$timestamp] Sleeping 60 seconds (1 min)..."
    sleep 60
done

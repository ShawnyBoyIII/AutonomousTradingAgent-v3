#!/bin/bash
# V2.5 Phase D FULLY AUTOMATED Burn-In with Dynamic Watchlist
# This runs discover → scan → trade → manage loop continuously
#
# Scan interval: 300 seconds (5 minutes) during market hours
# Discovery: HYBRID approach
#   - Daily at market open (9:30 AM ET) - ALWAYS
#   - Mid-day (12:00-1:00 PM ET) - ONLY if watchlist < 5 symbols
# Log rotation: Automatic when decision-log.jsonl exceeds 10MB

set -e

echo "=========================================="
echo "V2.5 FULLY AUTOMATED Paper Burn-In"
echo "Date: $(date)"
echo "Dynamic Watchlist: ENABLED"
echo "=========================================="
echo ""

cd /Users/shawndlima/Documents/AutonomousTradingAgentcopy

# Configuration
CONFIG_FILE="burn-in-config.yaml"
SYMBOLS_FILE="burn-in-symbols.txt"
LOG_DIR="logs/burn_in"
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
EOF
fi

# Create default symbols file if missing (will be overwritten by discover)
if [ ! -f "$SYMBOLS_FILE" ]; then
    echo "Creating initial symbol universe..."
    cat > "$SYMBOLS_FILE" << 'EOF'
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
    
    # Run discover with export
    local discover_output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" discover --mode breakout --max 15 --export 2>&1)
    
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
        
        # Log discovery event
        echo "{\"event\":\"discovery\",\"timestamp\":\"$timestamp\",\"trigger\":\"$trigger_reason\",\"count\":$count}" >> "$LOG_DIR/discovery.log"
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
    if [ ! -f "$SYMBOLS_FILE" ]; then
        return 0  # No file = definitely low
    fi
    local count=$(wc -l < "$SYMBOLS_FILE" | tr -d ' ')
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

# Load symbols function
load_symbols() {
    if [ -f "$SYMBOLS_FILE" ]; then
        SYMBOLS=$(grep -v "^#" "$SYMBOLS_FILE" | grep -v "^$" | tr '\n' ',' | sed 's/,$//')
    else
        SYMBOLS="SPY,QQQ,AAPL,MSFT,NVDA"
    fi
}

echo "Configuration:"
echo "  Config: $CONFIG_FILE"
echo "  Symbols File: $SYMBOLS_FILE"
echo "  Database: $DB_PATH"
echo "  Mode: Dynamic discovery + V3 signals"
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
echo "  3. Auto-trade GREEN signals (V3 strategy)"
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

# Main loop
echo "Starting automated loop..."
echo ""

# Track cycle count
CYCLE_COUNT=0

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
    
    # Show cycle summary every 10 cycles
    if [ $((CYCLE_COUNT % 10)) -eq 0 ]; then
        echo "[$timestamp] 📈 Completed $CYCLE_COUNT cycles"
    fi
    
    echo ""
    echo "[$timestamp] Sleeping 300 seconds (5 min)..."
    sleep 300
done

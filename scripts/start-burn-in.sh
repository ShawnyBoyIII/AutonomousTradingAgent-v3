#!/bin/bash
# V2.5 Phase D Burn-In Starter
# Starts the continuous paper trading daemon

set -e  # Exit on error

echo "=========================================="
echo "V2.5 Paper Burn-In Starter"
echo "Date: $(date)"
echo "=========================================="
echo ""

cd /Users/shawndlima/Documents/AutonomousTradingAgentcopy

# Check if burn-in is already running
if pgrep -f "run-manager.*burn-in" > /dev/null; then
    echo "⚠️  Burn-in appears to already be running!"
    echo "Check with: ps aux | grep run-manager"
    echo ""
    read -p "Do you want to stop it and restart? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Stopping existing burn-in..."
        pkill -f "run-manager.*burn-in"
        sleep 2
    else
        echo "Exiting without changes."
        exit 0
    fi
fi

# Check config exists
CONFIG_FILE="burn-in-config.yaml"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Creating default burn-in configuration..."
    cat > "$CONFIG_FILE" << 'EOF'
app:
  timezone: "America/New_York"
  state_db_path: "state/burn_in.db"
  log_dir: "logs/burn_in"
  portfolio_summary_path: "state/burn_in/portfolio_summary.json"
  scan_results_path: "state/burn_in/scan_results.json"
  dashboard_summary_path: "state/burn_in/dashboard_summary.json"

market_data:
  provider: "yfinance"
  daily_period: "1y"
  intraday_period: "5d"
  intraday_interval: "5m"
  max_data_age_hours: 72
  max_data_age_minutes: 30
  validate_data: true
  max_price_jump_pct: 1000.0
  max_volume_jump_pct: 1000.0
  min_bars_for_signal: 5

risk:
  max_risk_per_trade_pct: 0.01
  max_daily_risk_pct: 0.03
  max_daily_orders: 3
  max_ticker_allocation_pct: 0.20
  min_reward_risk_ratio: 2.0
  use_atr_sizing: true
  atr_period: 14
  atr_multiplier: 2.0
  max_portfolio_heat_pct: 0.03

session:
  close_hour: 16
  close_minute: 0
  eod_minutes_before_close: 5
  eod_enabled: true

paper:
  fee_per_order: 1.0
  slippage_bps: 0
EOF
    echo "✅ Created $CONFIG_FILE"
    echo ""
fi

# Check symbol universe exists
if [ ! -f "burn-in-symbols.txt" ]; then
    echo "⚠️  burn-in-symbols.txt not found!"
    echo "Creating with default symbols..."
    cat > burn-in-symbols.txt << 'EOF'
# Default Burn-In Universe
SPY
QQQ
AAPL
MSFT
GOOGL
AMZN
NVDA
JPM
JNJ
XOM
EOF
    echo "✅ Created burn-in-symbols.txt"
    echo "Edit this file to customize your trading universe"
    echo ""
fi

# Show configuration
echo "Configuration:"
echo "---------------"
echo "Config file: $CONFIG_FILE"
echo "Symbols file: burn-in-symbols.txt"
echo "Database: state/burn_in.db"
echo "Logs: logs/burn_in/"
echo ""

# Count symbols
symbol_count=$(grep -v "^#" burn-in-symbols.txt | grep -v "^$" | wc -l)
echo "Symbol universe: $symbol_count symbols"
echo ""

# Check system health
echo "Pre-flight Health Check:"
echo "------------------------"
if ! sh ./tradebot-local --config-path "$CONFIG_FILE" health > /dev/null 2>&1; then
    echo "❌ Health check failed!"
    echo "Run manually to see details:"
    echo "  sh ./tradebot-local --config-path $CONFIG_FILE health"
    exit 1
fi
echo "✅ System healthy"
echo ""

# Check kill switch status
echo "Kill Switch Status:"
echo "-------------------"
if ! sh ./tradebot-local --config-path "$CONFIG_FILE" kill-switch > /dev/null 2>&1; then
    echo "⚠️  Kill switch is ACTIVE - trading is halted!"
    echo "To resume: sh ./tradebot-local --config-path $CONFIG_FILE kill-switch --resume"
    exit 1
fi
echo "✅ Kill switch: Trading active"
echo ""

# Test scan
echo "Testing Market Connection:"
echo "--------------------------"
symbols=$(grep -v "^#" burn-in-symbols.txt | grep -v "^$" | head -3 | tr '\n' ',' | sed 's/,$//')
if ! sh ./tradebot-local --config-path "$CONFIG_FILE" scan --symbols "$symbols" --summary > /dev/null 2>&1; then
    echo "⚠️  Market scan failed - check internet connection"
    exit 1
fi
echo "✅ Market data accessible"
echo ""

# All checks passed
echo "✅ All pre-flight checks passed!"
echo ""

# Ask for confirmation
read -p "Start burn-in daemon? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

echo ""
echo "Starting burn-in daemon..."
echo "This will run continuously until you stop it with Ctrl-C"
echo ""
echo "Monitor with:"
echo "  sh ./scripts/burn-in-monitor.sh        (daily check)"
echo "  tail -f logs/burn_in/decision-log.jsonl (live log)"
echo ""
echo "Press Ctrl-C to stop"
echo "=========================================="
echo ""

# Start the daemon
# Use nohup so it survives terminal disconnect
# Log output to both console and file
exec sh ./tradebot-local \
    --config-path "$CONFIG_FILE" \
    run-manager \
    --interval 60 \
    2>&1 | tee -a "logs/burn_in/daemon.log"

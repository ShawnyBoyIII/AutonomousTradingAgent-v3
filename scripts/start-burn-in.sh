#!/bin/bash
# Burn-in starter with preflight checks.
#
# This is a thin wrapper around ./scripts/auto-burn-in.sh. The legacy
# version of this script only started the exits-only `run-manager`
# command, which could never open a position; auto-burn-in.sh runs
# the full scan -> paper-trade -> manage-positions cycle.
#
# Preflight: ensures the burn-in-config.yaml exists and that the
# symbol universe has at least one ticker.

set -e

echo "=========================================="
echo "Burn-In Starter (delegates to auto-burn-in.sh)"
echo "Date: $(date)"
echo "=========================================="
echo ""

# Ensure config exists
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

# Ensure symbol universe exists
UNIVERSE_FILE="state/universe.txt"
if [ ! -f "$UNIVERSE_FILE" ]; then
    mkdir -p "$(dirname "$UNIVERSE_FILE")"
    echo "Creating default symbol universe..."
    cat > "$UNIVERSE_FILE" << 'EOF'
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
    echo "✅ Created $UNIVERSE_FILE"
    echo ""
fi

symbol_count=$(grep -v "^#" "$UNIVERSE_FILE" | grep -v "^$" | wc -l | tr -d ' ')
echo "Symbol universe: $symbol_count symbols"
echo ""

if [ "$symbol_count" -eq 0 ]; then
    echo "❌ Universe is empty. Edit $UNIVERSE_FILE and add tickers."
    exit 1
fi

# Hand off to the real automation script
exec sh ./scripts/auto-burn-in.sh

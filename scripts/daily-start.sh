#!/bin/bash
# Daily trading workflow — run this each morning
# Usage: ./scripts/daily-start.sh

set -e

cd "$(dirname "$0")/.."

echo "=========================================="
echo "Daily Trading Workflow"
echo "Date: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=========================================="
echo ""

# Step 1: Build universe (run once, or daily to refresh)
echo "Step 1: Build universe (scans market for ranked symbols)"
echo "  Command: ./tradebot-local scan-universe"
echo ""

# Step 2: Serve dashboard
echo "Step 2: Start live dashboard"
echo "  Command: ./tradebot-local --config-path burn-in-config.yaml serve --port 8000"
echo "  Then open: http://localhost:8000"
echo ""

# Step 3: Morning scan
echo "Step 3: Morning scan (9:30 AM ET)"
echo "  Command: ./tradebot-local scan --why --summary"
echo ""

# Step 4: Paper trade
echo "Step 4: Submit trades (paper only)"
echo "  Command: ./tradebot-local paper-trade"
echo "  (reads state/universe.txt + state/watchlist.txt)"
echo ""

# Step 5: Manage positions
echo "Step 5: Position management (midday + EOD)"
echo "  Command: ./tradebot-local manage-positions"
echo ""

# Step 6: Portfolio summary
echo "Step 6: Portfolio summary"
echo "  Command: ./tradebot-local portfolio"
echo ""

# Step 7: Kill switch status
echo "Step 7: Kill switch status"
echo "  Command: ./tradebot-local kill-switch --status"
echo ""

echo "=========================================="
echo "Quick copy-paste commands:"
echo "=========================================="
echo ""
echo "# Start dashboard (in one terminal)"
echo "./tradebot-local --config-path burn-in-config.yaml serve --port 8000"
echo ""
echo "# Morning scan (9:30 AM ET)"
echo "./tradebot-local scan --why --summary"
echo ""
echo "# Submit trades"
echo "./tradebot-local paper-trade"
echo ""
echo "# Midday check"
echo "./tradebot-local manage-positions"
echo ""
echo "# EOD check (forces exits)"
echo "./tradebot-local manage-positions"
echo ""

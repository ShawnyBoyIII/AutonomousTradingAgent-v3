#!/bin/bash
# Daily trading reference card — printed, not executed.
#
# The production automated workflow is ./scripts/auto-burn-in.sh.
# This file is the human-readable equivalent for ad-hoc operators who
# want to drive the trading day by hand. The exit code is always 0.

set -e

cd "$(dirname "$0")/.."

echo "=========================================="
echo "Daily Trading Workflow (manual reference)"
echo "Date: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=========================================="
echo ""
echo "Production automation: ./scripts/auto-burn-in.sh (manages everything"
echo "below on a 60s cycle after pre-market). Use this card only for ad-hoc"
echo "spot checks or for live development against the dashboard."
echo ""

echo "Step 1: Build universe"
echo "  Command: ./tradebot-local build-universe --max 50 --export"
echo ""

echo "Step 2: Start the canonical dashboard"
echo "  Command: ./tradebot-local --config-path burn-in-config.yaml serve --port 8080"
echo "  Then open: http://localhost:8080"
echo ""

echo "Step 3: Spot scan a single symbol"
echo "  Command: ./tradebot-local scan --symbols SPY --why --summary"
echo ""

echo "Step 4: Submit paper trades for a single symbol"
echo "  Command: ./tradebot-local paper-trade --symbols SPY"
echo ""

echo "Step 5: Midday position check"
echo "  Command: ./tradebot-local manage-positions"
echo ""

echo "Step 6: Portfolio summary"
echo "  Command: ./tradebot-local portfolio"
echo ""

echo "Step 7: Kill switch status"
echo "  Command: ./tradebot-local kill-switch --status"
echo ""

echo "=========================================="
echo "Quick copy-paste commands:"
echo "=========================================="
echo ""
echo "# Start dashboard (in one terminal)"
echo "./tradebot-local --config-path burn-in-config.yaml serve --port 8080"
echo ""
echo "# Morning scan (single symbol)"
echo "./tradebot-local scan --symbols SPY --why --summary"
echo ""
echo "# Submit trades (single symbol)"
echo "./tradebot-local paper-trade --symbols SPY"
echo ""
echo "# Midday check"
echo "./tradebot-local manage-positions"
echo ""
echo "# EOD check (forces exits via EOD priority)"
echo "./tradebot-local manage-positions"
echo ""

exit 0

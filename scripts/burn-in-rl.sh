#!/bin/bash
# RL-based paper trading burn-in loop
# Usage: sh ./scripts/burn-in-rl.sh [SYMBOLS]
# Default symbols: SPY,AAPL,QQQ,MSFT

set -e

SYMBOLS="${1:-SPY,AAPL,QQQ,MSFT}"
SLEEP_SECS=60
LOG_DIR="logs"

mkdir -p "$LOG_DIR"

echo "=========================================="
echo "RL Paper Trading Burn-In"
echo "Symbols: $SYMBOLS"
echo "Interval: ${SLEEP_SECS}s"
echo "=========================================="

# Pre-flight checks
if ! ./tradebot-local doctor > /dev/null 2>&1; then
    echo "Doctor check failed"
    exit 1
fi
if ! ./tradebot-local kill-switch > /dev/null 2>&1; then
    echo "Kill switch is ACTIVE - cannot start"
    exit 1
fi

echo "Starting burn-in loop (Ctrl-C to stop)..."
while true; do
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

    # Scan for RL-approved signals
    SCAN_OUT=$(./tradebot-local scan --symbols "$SYMBOLS" --why 2>&1 || true)
    echo "[$TIMESTAMP] Scan output:"
    echo "$SCAN_OUT" | grep -E "APPROVED|NO_SIGNAL|REJECTED" || true

    # Trade any APPROVED symbols (RL bypasses the GREEN quality filter in paper-trade)
    APPROVED=$(echo "$SCAN_OUT" | grep "APPROVED" | awk '{print $1}' | tr '\n' ',' | sed 's/,$//')
    if [ -n "$APPROVED" ]; then
        echo "[$TIMESTAMP] Trading approved symbols: $APPROVED"
        IFS=',' read -ra SYM_ARRAY <<< "$APPROVED"
        for sym in "${SYM_ARRAY[@]}"; do
            ./tradebot-local paper-trade --symbols "$sym" 2>&1 | grep -E "FILLED|REJECTED|NO_SIGNAL" || true
        done
    fi

    # Manage existing positions
    echo "[$TIMESTAMP] Managing positions..."
    ./tradebot-local manage-positions 2>&1 | tail -5

    echo "[$TIMESTAMP] Sleeping ${SLEEP_SECS}s..."
    echo ""
    sleep "$SLEEP_SECS"
done

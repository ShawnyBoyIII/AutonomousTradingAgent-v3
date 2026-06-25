#!/bin/bash
# V2.5 Phase D Burn-In Monitor
# Uses the analytics module for proper JSON-based reporting

set -e

echo "=========================================="
echo "V2.5 Paper Burn-In Daily Check"
echo "Date: $(date)"
echo "=========================================="
echo ""

cd /Users/shawndlima/Documents/AutonomousTradingAgentcopy

CONFIG_FILE="burn-in-config.yaml"
LOG_DIR="logs/burn_in"
DECISION_LOG="$LOG_DIR/decision-log.jsonl"

echo "1. Burn-In Analytics Report:"
echo "----------------------------"
if [ -f "$DECISION_LOG" ] && [ -s "$DECISION_LOG" ]; then
    sh ./tradebot-local --config-path "$CONFIG_FILE" burn-in-report 2>&1 | head -60
else
    echo "No decision log found at $DECISION_LOG"
    echo "Burn-in may not have started or logs may be in a different location."
fi
echo ""

echo "2. Market Scan (Burn-In Universe):"
echo "-----------------------------------"
if [ -f burn-in-symbols.txt ]; then
    symbols=$(grep -v "^#" burn-in-symbols.txt | grep -v "^$" | tr '\n' ',' | sed 's/,$//')
    if [ -n "$symbols" ]; then
        echo "Scanning: $symbols"
        echo ""
        sh ./tradebot-local --config-path "$CONFIG_FILE" scan --symbols "$symbols" --summary 2>&1 | head -15
    else
        echo "No symbols found in burn-in-symbols.txt"
    fi
else
    echo "No universe file found - using default SPY"
    sh ./tradebot-local scan --symbols SPY --summary
fi
echo ""

echo "3. Kill Switch & Circuit Breaker:"
echo "---------------------------------"
sh ./tradebot-local --config-path "$CONFIG_FILE" kill-switch 2>&1 | head -5
echo ""

echo "4. Universe Status:"
echo "--------------------"
if [ -f burn-in-symbols.txt ]; then
    SYMBOL_COUNT=$(wc -l < burn-in-symbols.txt | tr -d ' ')
    echo "Universe size: $SYMBOL_COUNT symbols"
    
    if [ "$SYMBOL_COUNT" -lt 10 ]; then
        echo "⚠️  Universe small - consider running discovery"
    fi
else
    echo "No universe file found"
fi
echo ""

echo "=========================================="
echo "Daily Check Complete"
echo "=========================================="
echo ""
echo "Full analytics: sh ./tradebot-local --config-path $CONFIG_FILE burn-in-report"
echo "JSON output:    sh ./tradebot-local --config-path $CONFIG_FILE burn-in-report --json"
echo "Live log:       tail -f $LOG_DIR/decision-log.jsonl"
echo "Run discovery:  ./tradebot-local --config-path $CONFIG_FILE discover --mode breakout --max 20 --export"

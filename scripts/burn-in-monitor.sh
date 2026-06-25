#!/bin/bash
# V2.5 Phase D Burn-In Monitor
# Run this daily to check burn-in status

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

echo "1. System Health Check:"
echo "------------------------"
sh ./tradebot-local health 2>&1 | head -10
echo ""

echo "2. Active Alerts:"
echo "-----------------"
sh ./tradebot-local alerts 2>&1 | head -10
echo ""

echo "3. Market Scan (Burn-In Universe):"
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
    echo "burn-in-symbols.txt not found - using default SPY"
    sh ./tradebot-local scan --symbols SPY --summary
fi
echo ""

echo "4. Portfolio Status:"
echo "--------------------"
sh ./tradebot-local --config-path "$CONFIG_FILE" portfolio 2>&1 | head -10
echo ""

echo "5. Today's Performance:"
echo "----------------------"
TODAY=$(date '+%Y-%m-%d')
if [ -f "$DECISION_LOG" ]; then
    TODAY_TRADES=$(grep "\"status\": \"FILLED\"" "$DECISION_LOG" 2>/dev/null | grep "$TODAY" | wc -l | tr -d ' ')
    TODAY_WINS=$(grep "\"status\": \"FILLED\"" "$DECISION_LOG" 2>/dev/null | grep "$TODAY" | grep -E '"pnl":\s*[0-9]+\.[0-9]+' | wc -l | tr -d ' ')
    TODAY_LOSSES=$(grep "\"status\": \"FILLED\"" "$DECISION_LOG" 2>/dev/null | grep "$TODAY" | grep -E '"pnl":\s*-[0-9]+\.[0-9]+' | wc -l | tr -d ' ')
    
    echo "Trades today: $TODAY_TRADES"
    echo "Wins: $TODAY_WINS | Losses: $TODAY_LOSSES"
    
    if [ "$TODAY_TRADES" -gt 0 ]; then
        echo "Win rate today: $(echo "scale=0; $TODAY_WINS * 100 / $TODAY_TRADES" | bc 2>/dev/null || echo "N/A")%"
    fi
else
    echo "No trades yet"
fi
echo ""

echo "6. Kill Switch & Circuit Breaker:"
echo "---------------------------------"
sh ./tradebot-local --config-path "$CONFIG_FILE" kill-switch 2>&1 | head -5
echo ""

echo "7. Recent Trades (Last 5):"
echo "--------------------------"
if [ -f "$DECISION_LOG" ]; then
    grep '"status": "FILLED"' "$DECISION_LOG" 2>/dev/null | tail -5 || echo "No trades yet"
else
    echo "No decision log found"
fi
echo ""

echo "8. Counter-Thesis Activity (V3):"
echo "---------------------------------"
if [ -f "$DECISION_LOG" ]; then
    CT_BLOCKS=$(grep '"counter_thesis_block":\s*true' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
    CT_SCALED=$(grep '"confidence_multiplier"' "$DECISION_LOG" 2>/dev/null | grep -v '"confidence_multiplier": 1.0' | wc -l | tr -d ' ')
    
    echo "Total blocks: $CT_BLOCKS | Total scaled: $CT_SCALED"
    
    if [ "$CT_BLOCKS" -gt 0 ]; then
        echo "Recent blocks:"
        grep '"counter_thesis_block":\s*true' "$DECISION_LOG" 2>/dev/null | tail -2
    fi
else
    echo "No data"
fi
echo ""

echo "9. Position Management Today:"
echo "----------------------------"
if [ -f "$DECISION_LOG" ]; then
    EOD=$(grep '"exit_type":\s*"eod"' "$DECISION_LOG" 2>/dev/null | grep "$TODAY" | wc -l | tr -d ' ')
    STOPS=$(grep '"exit_type":\s*"stop_loss"' "$DECISION_LOG" 2>/dev/null | grep "$TODAY" | wc -l | tr -d ' ')
    TARGETS=$(grep '"exit_type":\s*"profit_target"' "$DECISION_LOG" 2>/dev/null | grep "$TODAY" | wc -l | tr -d ' ')
    
    echo "EOD exits: $EOD | Stop exits: $STOPS | Target exits: $TARGETS"
else
    echo "No data"
fi
echo ""

echo "10. Universe Status:"
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
echo "Quick commands:"
echo "  Weekly review: sh ./scripts/burn-in-weekly-review.sh"
echo "  Live log:      tail -f $LOG_DIR/decision-log.jsonl"
echo "  Run discovery: ./tradebot-local --config-path $CONFIG_FILE discover --mode breakout --max 20 --export"

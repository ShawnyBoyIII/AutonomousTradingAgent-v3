#!/bin/bash
# V2.5 Phase D Burn-In Weekly Review
# Run this weekly (Friday after close or weekend) for comprehensive analysis

set -e

echo "=========================================="
echo "V2.5 Paper Burn-In WEEKLY Review"
echo "Week ending: $(date '+%Y-%m-%d')"
echo "=========================================="
echo ""

cd /Users/shawndlima/Documents/AutonomousTradingAgentcopy

CONFIG_FILE="burn-in-config.yaml"
LOG_DIR="logs/burn_in"
DECISION_LOG="$LOG_DIR/decision-log.jsonl"
OUTPUT_FILE="$LOG_DIR/weekly-review-$(date '+%Y-%m-%d').json"

# Ensure log directory exists
mkdir -p "$LOG_DIR"

echo "1. Weekly Performance Summary"
echo "-----------------------------"

# Count trades this week (last 7 days)
if [ -f "$DECISION_LOG" ]; then
    WEEK_AGO=$(date -v-7d '+%Y-%m-%d' 2>/dev/null || date -d '7 days ago' '+%Y-%m-%d' 2>/dev/null || echo "")
    
    if [ -n "$WEEK_AGO" ]; then
        # Count FILLED events
        FILLED_COUNT=$(grep '"status": "FILLED"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
        
        # Count wins (positive P&L) and losses
        WINS=$(grep '"status": "FILLED"' "$DECISION_LOG" 2>/dev/null | grep -E '"pnl":\s*[0-9]+\.[0-9]+' | wc -l | tr -d ' ' || echo "0")
        LOSSES=$(grep '"status": "FILLED"' "$DECISION_LOG" 2>/dev/null | grep -E '"pnl":\s*-[0-9]+\.[0-9]+' | wc -l | tr -d ' ' || echo "0")
        
        echo "Total trades: $FILLED_COUNT"
        echo "Wins: $WINS"
        echo "Losses: $LOSSES"
        
        if [ "$FILLED_COUNT" -gt 0 ]; then
            WIN_RATE=$(echo "scale=2; $WINS * 100 / $FILLED_COUNT" | bc 2>/dev/null || echo "N/A")
            echo "Win rate: ${WIN_RATE}%"
        fi
    else
        echo "Total trades: $(grep '"status": "FILLED"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')"
    fi
else
    echo "No decision log found"
fi
echo ""

echo "2. Trade Distribution by Ticker"
echo "--------------------------------"
if [ -f "$DECISION_LOG" ]; then
    grep '"status": "FILLED"' "$DECISION_LOG" 2>/dev/null | \
        grep -oE '"ticker":\s*"[A-Z-]+"' | \
        sed 's/"ticker":\s*"\([^"]*\)"/\1/' | \
        sort | uniq -c | sort -rn | head -10
fi
echo ""

echo "3. Signal Quality Analysis"
echo "--------------------------"
if [ -f "$DECISION_LOG" ]; then
    # Count by signal quality
    echo "GREEN signals: $(grep '"quality":\s*"GREEN"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')"
    echo "YELLOW signals: $(grep '"quality":\s*"YELLOW"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')"
    
    # Count rejections by reason
    echo ""
    echo "Top rejection reasons:"
    grep '"status": "REJECTED"' "$DECISION_LOG" 2>/dev/null | \
        sed 's/.*"reason":\s*"\([^"]*\)".*/\1/' | \
        sort | uniq -c | sort -rn | head -5
fi
echo ""

echo "4. Counter-Thesis Impact (V3)"
echo "-----------------------------"
if [ -f "$DECISION_LOG" ]; then
    # Count counter-thesis blocks
    CT_BLOCKS=$(grep '"counter_thesis_block":\s*true' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
    CT_SCALED=$(grep '"confidence_multiplier"' "$DECISION_LOG" 2>/dev/null | grep -v '1.0' | wc -l | tr -d ' ')
    
    echo "Trades blocked by counter-thesis: $CT_BLOCKS"
    echo "Trades scaled by counter-thesis: $CT_SCALED"
    
    # Show blocked tickers
    if [ "$CT_BLOCKS" -gt 0 ]; then
        echo ""
        echo "Blocked tickers:"
        grep '"counter_thesis_block":\s*true' "$DECISION_LOG" 2>/dev/null | \
            sed 's/.*"ticker":\s*"\([^"]*\)".*/\1/' | \
            sort | uniq -c | sort -rn | head -5
    fi
fi
echo ""

echo "5. Risk Metrics"
echo "---------------"
# Get current portfolio state
if [ -f "state/burn_in.db" ]; then
    echo "Portfolio heat check..."
    .venv/bin/python -m trading_bot.main portfolio 2>&1 | grep -E "(equity|cash|unrealized)" | head -5
fi
echo ""

echo "6. Data Validation Issues"
echo "-------------------------"
if [ -f "$DECISION_LOG" ]; then
    VALIDATION_ERRORS=$(grep '"status": "VALIDATION_ERROR"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
    STALE_DATA=$(grep '"reason":.*stale' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
    
    echo "Validation errors: $VALIDATION_ERRORS"
    echo "Stale data skips: $STALE_DATA"
    
    if [ "$VALIDATION_ERRORS" -gt 0 ]; then
        echo ""
        echo "Recent validation errors:"
        grep '"status": "VALIDATION_ERROR"' "$DECISION_LOG" 2>/dev/null | tail -3
    fi
fi
echo ""

echo "7. Kill Switch & Circuit Breaker Events"
echo "----------------------------------------"
if [ -f "$DECISION_LOG" ]; then
    KS_EVENTS=$(grep '"status": "KILL_SWITCH"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
    CB_EVENTS=$(grep '"status": "CIRCUIT_BREAKER"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
    
    echo "Kill switch triggers: $KS_EVENTS"
    echo "Circuit breaker triggers: $CB_EVENTS"
    
    if [ "$CB_EVENTS" -gt 0 ]; then
        echo ""
        echo "Circuit breaker reasons:"
        grep '"status": "CIRCUIT_BREAKER"' "$DECISION_LOG" 2>/dev/null | \
            sed 's/.*"reason":\s*"\([^"]*\)".*/\1/' | \
            sort | uniq -c | sort -rn
    fi
fi
echo ""

echo "8. Position Management Actions"
echo "-------------------------------"
if [ -f "$DECISION_LOG" ]; then
    # Count exits by type
    EOD_EXITS=$(grep '"exit_type":\s*"eod"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
    STOP_EXITS=$(grep '"exit_type":\s*"stop_loss"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
    TARGET_EXITS=$(grep '"exit_type":\s*"profit_target"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
    TRAILING_EXITS=$(grep '"exit_type":\s*"trailing_stop"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
    CT_EXITS=$(grep '"exit_type":\s*"counter_thesis"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
    
    echo "EOD exits: $EOD_EXITS"
    echo "Stop loss exits: $STOP_EXITS"
    echo "Profit target exits: $TARGET_EXITS"
    echo "Trailing stop exits: $TRAILING_EXITS"
    echo "Counter-thesis exits: $CT_EXITS"
fi
echo ""

echo "9. Discovery & Universe Changes"
echo "--------------------------------"
if [ -f "$LOG_DIR/discovery.log" ]; then
    DISCOVERIES=$(wc -l < "$LOG_DIR/discovery.log" | tr -d ' ')
    echo "Discovery events this week: $DISCOVERIES"
    
    echo ""
    echo "Recent discoveries:"
    tail -5 "$LOG_DIR/discovery.log" 2>/dev/null
fi
echo ""

echo "10. Recommendations for Next Week"
echo "----------------------------------"

# Analyze and provide recommendations
if [ -f "$DECISION_LOG" ]; then
    FILLED_COUNT=$(grep '"status": "FILLED"' "$DECISION_LOG" 2>/dev/null | wc -l | tr -d ' ')
    
    if [ "$FILLED_COUNT" -lt 5 ]; then
        echo "⚠️  Low trade frequency - consider:"
        echo "   - Expanding universe (run: ./tradebot-local discover --mode breakout --max 30)"
        echo "   - Relaxing confidence threshold in burn-in-config.yaml"
        echo "   - Checking if market regime is unfavorable"
    fi
    
    if [ "$CT_BLOCKS" -gt 5 ]; then
        echo "⚠️  High counter-thesis block rate - review:"
        echo "   - Counter-thesis thresholds in config"
        echo "   - Market regime alignment"
    fi
    
    if [ "$STOP_EXITS" -gt "$TARGET_EXITS" ]; then
        echo "⚠️  More stop exits than target exits - consider:"
        echo "   - Tightening entry criteria"
        echo "   - Adjusting profit target / stop loss ratio"
    fi
fi

echo ""
echo "=========================================="
echo "Weekly Review Complete"
echo "=========================================="
echo ""
echo "Save this report: $OUTPUT_FILE"
echo ""
echo "Next steps:"
echo "  - Review underperforming tickers"
echo "  - Adjust strategy parameters if needed"
echo "  - Run discovery if universe < 10 symbols"
echo "  - Check state/universe.txt for next week"

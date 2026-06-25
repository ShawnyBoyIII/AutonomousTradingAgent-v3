#!/usr/bin/env bash
# Quick RL training script for the trading agent.
# Usage: ./scripts/quick-train.sh [symbols] [timesteps]
#
# Examples:
#   ./scripts/quick-train.sh              # AAPL, 50k timesteps
#   ./scripts/quick-train.sh AAPL,MSFT    # AAPL+MSFT, 50k timesteps
#   ./scripts/quick-train.sh AAPL,MSFT,GOOGL 100000  # 100k timesteps
#   ./scripts/quick-train.sh --evaluate   # Evaluate existing model

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Handle help flag
for arg in "$@"; do
    if [[ "$arg" == "--help" || "$arg" == "-h" ]]; then
        echo "Usage: ./scripts/quick-train.sh [symbols] [timesteps]"
        echo ""
        echo "Examples:"
        echo "  ./scripts/quick-train.sh              # AAPL, 50k timesteps"
        echo "  ./scripts/quick-train.sh AAPL,MSFT    # AAPL+MSFT, 50k timesteps"
        echo "  ./scripts/quick-train.sh AAPL,MSFT,GOOGL 100000  # 100k timesteps"
        echo "  ./scripts/quick-train.sh --evaluate   # Evaluate existing model"
        exit 0
    fi
done

SYMBOLS="${1:-AAPL}"
TIMESTEPS="${2:-50000}"
EVAL_MODE=""

if [[ "$SYMBOLS" == "--evaluate" ]]; then
    EVAL_MODE="--evaluate"
    SYMBOLS="AAPL"
fi

echo "============================================================"
echo "  RL Training"
echo "============================================================"
echo "  Symbols:    $SYMBOLS"
echo "  Timesteps:  $TIMESTEPS"
echo "  Agent:      PPO"
echo "  Output:     state/rl_logs/PPO_final"
echo "============================================================"
echo ""

cd "$PROJECT_DIR"
./tradebot-local rl-train \
    --symbols "$SYMBOLS" \
    --timesteps "$TIMESTEPS" \
    $EVAL_MODE

echo ""
echo "============================================================"
echo "  Done!"
echo "============================================================"
echo ""
if [[ -z "$EVAL_MODE" ]]; then
    echo "Next steps:"
    echo "  1. Compare strategies: ./tradebot-local backtest --symbols $SYMBOLS --compare"
    echo "  2. Evaluate model:     ./tradebot-local rl-train --symbols $SYMBOLS --evaluate"
    echo "  3. Run backtest:       ./tradebot-local backtest --symbols $SYMBOLS --strategy rl"
fi

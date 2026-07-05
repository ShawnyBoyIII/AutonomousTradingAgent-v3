#!/bin/bash
# Weekly RL model retraining script
# Run Sunday evening (5:00 PM ET) after market close
# Usage: ./scripts/retrain-rl-weekly.sh [--dry-run]

set -e

cd "$(dirname "$0")/.."
DRY_RUN=false
if [ "$1" = "--dry-run" ]; then DRY_RUN=true; fi

TIMESTAMP=$(date '+%Y-%m-%d_%H%M')
OUTPUT_DIR="state/rl_logs/weekly_${TIMESTAMP}"
LOG_FILE="logs/rl_retrain_${TIMESTAMP}.log"

echo "=========================================="
echo "Weekly RL Model Retraining"
echo "Date: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "Output: $OUTPUT_DIR"
echo "=========================================="

if $DRY_RUN; then
    echo "[DRY RUN] Would create: $OUTPUT_DIR"
    echo "[DRY RUN] Would train on $(wc -l < state/universe.txt | tr -d ' ') universe symbols"
    exit 0
fi

# Get current universe symbols
SYMBOLS=$(cat state/universe.txt 2>/dev/null | tr -d '[:space:]' | tr '\n' ',' | sed 's/,$//')
SYMBOL_COUNT=$(echo "$SYMBOLS" | tr ',' '\n' | wc -l | tr -d ' ')
if [ "$SYMBOL_COUNT" -lt 5 ]; then
    echo "Universe has only $SYMBOL_COUNT symbols, adding fallback symbols"
    SYMBOLS="EBS,SOFI,PRTA,GCTS,TNXP,CWH,AVXL,UTZ,NOK,MED"
fi

echo "Training on $SYMBOL_COUNT symbols: ${SYMBOLS:0:80}..."

mkdir -p "$OUTPUT_DIR"

for SEED in 789 42 123; do
    echo ""
    echo "--- Seed $SEED ---"
    .venv/bin/python -c "
from trading_bot.rl.trainer import RLTrainer, TrainingConfig
from trading_bot.rl.env import TradingConfig
import json, time, os

symbols = [s.strip() for s in '$SYMBOLS'.split(',') if s.strip()]
env_config = TradingConfig(
    symbols=symbols, bar_period='1y', bar_interval='1d',
    reward_scheme='risk_adjusted',
    data_end_date='$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d "yesterday" +%Y-%m-%d)',
    starting_cash=100000.0, max_positions=8,
)
train_config = TrainingConfig(
    env_config=env_config, model_type='PPO',
    total_timesteps=200000, seed=$SEED,
    log_dir='$OUTPUT_DIR', verbose=0,
    learning_rate=3e-4, ent_coef=0.01,
)
print(f'Training seed $SEED on {len(symbols)} symbols...')
start = time.time()
trainer = RLTrainer(train_config)
model = trainer.train()
elapsed = time.time() - start
print(f'Done in {elapsed:.0f}s ({elapsed/60:.1f}min)')

# Rename and write metadata
src = f'$OUTPUT_DIR/PPO_final.zip'
dst = f'$OUTPUT_DIR/PPO_seed_$SEED.zip'
if os.path.exists(src):
    os.rename(src, dst)
meta = {
    'symbols': symbols, 'seed': $SEED, 'agent': 'PPO',
    'total_timesteps': 200000, 'reward_scheme': 'risk_adjusted',
    'data_end_date': '$(date +%Y-%m-%d)',
    'trained_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
}
with open(f'$OUTPUT_DIR/PPO_seed_$SEED_meta.json', 'w') as f:
    json.dump(meta, f, indent=2)
" >> "$LOG_FILE" 2>&1
done

# Update burn-in config symlink
if [ -f "$OUTPUT_DIR/PPO_seed_789.zip" ] && [ -f "$OUTPUT_DIR/PPO_seed_42.zip" ] && [ -f "$OUTPUT_DIR/PPO_seed_123.zip" ]; then
    # Update model paths in config
    if [ -f burn-in-config.yaml ]; then
        sed -i '' "s|model_path: .*|model_path: \"$OUTPUT_DIR/PPO_seed_789.zip\"|" burn-in-config.yaml 2>/dev/null || true
        echo "Config updated with new model paths"
    fi
    echo "✅ Weekly retrain complete: $OUTPUT_DIR"
else
    echo "❌ Training incomplete - not all seeds succeeded"
    exit 1
fi

echo ""
echo "Model files:"
ls -la "$OUTPUT_DIR"/*.zip
echo ""
echo "Log: $LOG_FILE"
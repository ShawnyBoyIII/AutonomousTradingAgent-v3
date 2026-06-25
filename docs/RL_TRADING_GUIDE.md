# RL Trading Agent Guide

## Overview

The Autonomous Trading Agent now supports **Deep Reinforcement Learning (DRL)** for signal generation. This integration allows you to train agents that learn optimal trading policies from historical data.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  RL Integration Layers                                  │
├─────────────────────────────────────────────────────────┤
│  CLI Commands      │  rl-train, rl-eval                │
│  Orchestrator      │  RL signal path in orchestrator   │
│  Agent Wrapper     │  RLAgent (stable-baselines3)      │
│  Environment       │  TradingEnv (Gymnasium)           │
│  Features          │  FeatureEngineer + GroupByScaler  │
└─────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Install RL Dependencies

```bash
./tradebot-local doctor  # Verify base installation
.venv/bin/pip install -e ".[rl]"
```

### 2. Train an Agent

```bash
# Train PPO agent on AAPL
./tradebot-local rl-train --symbols AAPL --agent PPO --episodes 100 --timesteps 100000

# Train with custom learning rate
./tradebot-local rl-train --symbols AAPL --agent PPO --learning-rate 0.001

# Train extended feature set
./tradebot-local rl-train --symbols AAPL --feature-set extended
```

### 3. Evaluate Agent

```bash
./tradebot-local rl-eval --symbols AAPL --episodes 10
```

### 4. Enable RL for Trading

Add to your config.yaml:

```yaml
rl:
  enabled: true
  agent_type: "PPO"
  feature_set: "standard"
  model_path: "trained_models/rl_agent_AAPL_PPO.zip"
  action_confidence_threshold: 0.5
```

Then run:

```bash
./tradebot-local scan --symbols AAPL
./tradebot-local paper-trade --symbols AAPL
```

## Components

### FeatureEngineer (`trading_bot/data/feature_engineering.py`)

Extracts and normalizes features for RL input:

- **Standard (19 features):** log returns, price vs MAs, BB%, RSI, MACD, Stochastic, CCI, Williams %R, ATR%, ADX, volume ratio, OBV change, VWAP distance + 3 portfolio state
- **Extended (24 features):** Standard + RSI7, Stoch D, +DI/-DI, BB width

Uses **GroupByScaler** for per-ticker z-score normalization.

### TradingEnv (`trading_bot/env/trading_env.py`)

Gymnasium environment wrapping PaperBroker:

- **Action Space:** `Discrete(3)` - HOLD(0), BUY(1), SELL(2)
- **Observation Space:** `Box(19)` or `Box(24)` depending on feature set
- **Reward:** Portfolio return change, penalized for transaction costs and drawdown

### RLAgent (`trading_bot/rl/agent.py`)

Wrapper for stable-baselines3 agents:

- Supported agents: PPO, A2C, SAC, TD3, DDPG
- Methods: `train()`, `predict()`, `save()`, `load()`, `evaluate()`

## Training Best Practices

### Data Quality
- Use at least 1 year of daily data
- Ensure indicators are computed on training data
- Avoid look-ahead bias (features use only past data)

### Hyperparameters

| Agent | Learning Rate | Timesteps | Notes |
|-------|--------------|-----------|-------|
| PPO   | 3e-4         | 100k-500k | Default, stable |
| A2C   | 7e-4         | 100k-300k | Faster training |
| SAC   | 3e-4         | 200k-500k | Continuous actions |
| TD3   | 1e-3         | 200k-500k | Good for noisy envs |

### Reward Functions

- **`pnl`:** Raw PnL change (simple, but volatile)
- **`sharpe`:** Risk-adjusted returns (recommended)
- **`sortino`:** Downside risk adjustment

### Evaluation Metrics

- **Win Rate:** % of episodes with positive returns
- **Avg Reward:** Mean episode reward
- **Final Equity:** Portfolio value at episode end
- **Sharpe Ratio:** Risk-adjusted performance

## Integration with Existing System

### Signal Flow (RL Enabled)

```
1. orchestrator._build_rl_signal_result()
   └── Fetch daily + intraday bars
   └── Add indicators
   └── Load trained RL model
   └── agent.predict() → action, confidence
   └── If BUY: create TradeSignal with stop/target
   └── If HOLD/SELL: return None

2. evaluate_signal() (risk manager)
   └── Check portfolio heat
   └── Check counter-thesis (if enabled)
   └── Calculate position size (ATR-based)
   └── Return approval decision

3. submit_signal_as_order()
   └── Create OrderRequest
   └── PaperBroker fills order
   └── Update ledger
```

### Safety Constraints

RL agents operate within existing safety limits:

- **Kill switch:** Blocks all trading if enabled
- **Circuit breaker:** Halts on consecutive losses
- **Portfolio heat:** Max 3% unrealized loss
- **Position sizing:** Max 20% per ticker, ATR-adjusted
- **Confidence threshold:** Actions below threshold rejected

## Example Configurations

### Conservative RL Trading

```yaml
rl:
  enabled: true
  agent_type: "PPO"
  feature_set: "standard"
  model_path: "trained_models/rl_agent_conservative.zip"
  action_confidence_threshold: 0.7  # High confidence required
  max_position_pct: 0.10  # 10% max per position

risk:
  max_risk_per_trade_pct: 0.005  # 0.5% risk per trade
  max_portfolio_heat_pct: 0.02  # 2% max heat
```

### Aggressive RL Trading

```yaml
rl:
  enabled: true
  agent_type: "SAC"
  feature_set: "extended"
  model_path: "trained_models/rl_agent_aggressive.zip"
  action_confidence_threshold: 0.4  # Lower threshold
  max_position_pct: 0.20  # 20% max per position

risk:
  max_risk_per_trade_pct: 0.02  # 2% risk per trade
  atr_multiplier: 1.5  # Tighter stops
```

### Multi-Agent Ensemble

Train separate agents on different symbols:

```bash
./tradebot-local rl-train --symbols AAPL --agent PPO
./tradebot-local rl-train --symbols SPY --agent A2C
./tradebot-local rl-train --symbols QQQ --agent SAC
```

Then use symbol-specific model paths in config.

## Troubleshooting

### "Model not found"
- Ensure `model_path` is relative to project root
- Check file exists: `ls trained_models/rl_agent_*.zip`

### "stable-baselines3 not installed"
```bash
.venv/bin/pip install gymnasium stable-baselines3 torch
```

### Poor Performance
- Increase training timesteps (200k-500k)
- Try different agent (A2C trains faster, PPO more stable)
- Adjust learning rate (lower = more stable, higher = faster)
- Use extended feature set for more information

### NaN in Observations
- Ensure all indicators computed on training data
- Check for insufficient history (< 50 bars)
- Verify volume data exists (required for VWAP, volume_ratio)

## Performance Benchmarks

Typical training times (M3 Mac, 1 year daily data):

| Agent | Timesteps | Time | Memory |
|-------|-----------|------|--------|
| PPO   | 100k      | 2-5 min | 200 MB |
| A2C   | 100k      | 1-3 min | 150 MB |
| SAC   | 200k      | 5-10 min | 400 MB |

## Next Steps

1. **Train baseline agent:** Start with PPO on AAPL
2. **Evaluate:** Compare vs rule-based signals
3. **Tune:** Adjust hyperparameters based on evaluation
4. **Deploy:** Enable in config with confidence threshold
5. **Monitor:** Track RL vs non-RL performance in burn-in logs

## References

- FinRL: https://github.com/AI4Finance-Foundation/FinRL
- Stable Baselines3: https://stable-baselines3.readthedocs.io/
- Gymnasium: https://gymnasium.farama.org/

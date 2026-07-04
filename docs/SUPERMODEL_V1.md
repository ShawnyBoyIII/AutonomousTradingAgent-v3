# Supermodel V1

Smallest useful version: a read-only advisory layer that stacks evidence already produced by the bot.

## What It Does

- Combines local setup confidence, V3 confluence, RL vote, and counter-thesis guardrails.
- Emits `supermodel=<decision>:<score>` in `scan --why` when a real local signal exists.
- Adds summary counts when details are requested: support, caution, block, no_signal.
- Changes no trade execution, no sizing, no Robinhood path, and no live behavior.

## Decisions

- `support`: layers mostly agree and score is strong.
- `caution`: setup is usable but not enough agreement for high trust.
- `block`: one layer vetoes or average evidence is weak.
- `no_signal`: no local trade setup existed, so the stack does not pretend otherwise.

## Run

```bash
./tradebot-local scan --symbols AAPL,MSFT,NVDA --why --summary
```

Look for:

```text
supermodel=support:0.78 supermodel_layers=setup:support:0.82,v3:support:0.79,rl:support:0.72
```

## Next Gates

- Keep this advisory until paper logs show better trade selection than V2.5/V3 alone.
- Promote only after walk-forward diagnostics show stable expectancy, profit factor, drawdown, and enough trades.
- Later, let the stack influence paper trade eligibility before any live Robinhood intent path.

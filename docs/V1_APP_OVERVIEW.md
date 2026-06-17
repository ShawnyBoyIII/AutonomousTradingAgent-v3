# V1 App Overview

## What This App Does

Autonomous Trading Agent is a local, CLI-first stock and ETF research bot.

V1 can:

- Scan symbols for long trade candidates.
- Explain why each symbol is approved or rejected.
- Mark approved candidates as `GREEN` or `YELLOW`.
- Paper trade only fresh `GREEN` signals.
- Track local paper portfolio state.
- Produce local reports.
- Replay historical data with a simple backtest.
- Write JSON snapshots for future UI work.

V1 is paper-only by default. It does not place live broker orders.

## Main Commands

```bash
sh ./tradebot-local scan --symbols AAPL,MSFT,SPY,NVDA,QQQ --why
sh ./tradebot-local paper-trade --symbols SPY
sh ./tradebot-local backtest --symbols AAPL,MSFT,SPY,NVDA,QQQ --start 2026-05-01 --end 2026-06-17
sh ./tradebot-local portfolio
sh ./tradebot-local report
```

Use `tradebot-local` so the app runs from this repo's `.venv`, not a stale global install.

## Scan Logic

Scan uses daily trend plus intraday setup.

Daily filter:

- Price must be in bullish daily regime.
- If daily regime fails, result is `NO_SIGNAL reason=daily regime not bullish`.

Intraday setup:

- Breakout: close clears recent range high with volume support.
- Momentum continuation: price is moving up and closing strong enough inside the candle.
- If intraday setup fails, result is `NO_SIGNAL reason=no intraday setup`.

`--why` adds gate values:

- `daily_close`
- `ema_20`
- `sma_50`
- `intraday_close`
- `range_high`
- `volume`
- `volume_avg`
- `volume_ratio`

## Signal Quality

Approved signals get quality label.

`GREEN` means:

- Intraday close is above recent range high.
- `volume_ratio >= 1.00`.

`YELLOW` means:

- Signal passed strategy rules, but confirmation is weaker.
- Example: momentum setup below range high or volume below average.

## Paper Trading Guardrails

Paper trading is stricter than scan.

Paper trade rejects:

- No signal.
- Stale market data.
- `YELLOW` signals.
- Duplicate open ticker.
- Risk-manager rejection.
- Insufficient cash.
- Broker/order rejection.

Only fresh `GREEN` signals can fill.

## State And Snapshots

Runtime files are local and ignored by git.

Generated under `state/`:

- `scan_results.json`
- `portfolio_summary.json`
- `dashboard_summary.json`
- `backtest_summary.json`
- `trading_bot.db`

Generated under `logs/`:

- `decision-log.jsonl`

These files are replaceable local runtime artifacts.

## Current V1 Status

V1 is good as local trading research and paper-trading base.

Strengths:

- Clean local runner.
- Explainable scan output.
- Conservative paper-trade guardrails.
- JSON snapshots ready for future UI.
- Tests cover core CLI, strategy, risk, paper broker, portfolio, and backtest.

Limits:

- Market data can be stale or imperfect.
- Strategy is simple.
- Backtest is useful but not institution-grade.
- No live trading.
- No frontend yet.
- No macro or breadth model yet.

## Safety Position

V1 should not auto-trade real money.

Use it for:

- Research.
- Signal review.
- Paper trading.
- Dashboard/UI foundation.

Do not use it for:

- Live execution.
- Unsupervised trading.
- Large capital decisions.

## Next Best Work

Future work priority:

1. Add daily loss limit and daily order limit.
2. Add paper-trade dry-run preview before committing state changes.
3. Build simple dashboard from snapshot JSON files.

Reason:

- Daily loss/order limits prevent rogue local loops from creating too many simulated orders.
- Dry-run preview gives final sanity check before writing fills to SQLite.
- Dashboard makes paper portfolio debugging easier than terminal-only inspection.

Later:

- Add scan summary table.

## V2 Keep In Mind

Close trade lifecycle loop:

- Add manage-positions loop for open trades.
- Start with single-shot `manage-positions`; add continuous `run-manager --interval 60s` only after single-shot is stable.
- Trail stop loss behind moving averages or recent pivot lows.
- Add hard end-of-day exit for intraday positions around 3:55 PM ET.
- Avoid overnight gap risk for intraday momentum trades.

Position manager blueprint:

- Read open positions from SQLite.
- Fetch latest market data only for open tickers.
- Check end-of-day exit before all price logic.
- Check hard stop and target after time exit.
- Update trailing stop only after time, stop, and target checks pass.
- Dispatch generated sell signals through paper broker.
- Save updated portfolio state and refresh snapshots.

Exit order:

1. Hydrate open positions and latest data.
2. Trigger EOD liquidation first.
3. Trigger hard stop or target.
4. Ratchet trailing stop up only.
5. Commit sells and state updates.

Trailing stop methods:

- R-multiple ratchet: at `+1R`, move stop to breakeven; at `+1.5R`, move stop to `+0.5R`.
- Chandelier exit: track highest high since entry and set stop to `highest_high - (1.5 * ATR)`.

Manager guardrails:

- Freeze manager if market data is older than strict latency limit.
- Do not execute trailing stops or EOD exits on stale data.
- Handle SQLite locks cleanly if scanner and manager run near same time.

Harden paper simulation:

- Add strict latency gate between signal data timestamp and local clock.
- Reject signals older than configured latency limit.
- Simulate hostile fills with slippage.
- Deduct fees from portfolio state.
- Make paper trading prove edge survives friction.

Advanced filters and sizing:

- Add macro/breadth master gate.
- If SPY or QQQ are bearish daily, block long equity entries.
- Add ATR-based sizing.
- Size fewer shares for high-volatility names and more for slow movers.

Observability and developer experience:

- Add push notifications for `GREEN` signals and fills.
- Prefer Discord or Telegram webhook first.
- Build local dashboard from existing JSON snapshots.
- Keep dashboard simple before adding API/service layer.

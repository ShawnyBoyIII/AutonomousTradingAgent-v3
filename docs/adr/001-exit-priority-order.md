# ADR-001: Exit Priority Order (EOD → Stop → Target → Trail)

**Status:** Accepted  
**Date:** 2026-06-18  
**Deciders:** V2 Implementation Team  

## Context

V2 of the trading bot introduces multiple exit triggers:
- **EOD (End-of-Day):** Close all positions at market close to avoid overnight risk
- **Stop Loss:** Hard stop to limit downside
- **Profit Target:** Take profit at predetermined level
- **Trailing Stop:** Lock in gains while allowing upside

When multiple triggers fire simultaneously for the same position, we must define an unambiguous priority to ensure deterministic behavior.

## Decision

Exit priority order is: **EOD → Stop → Target → Trail**

This means:
1. **EOD exits run first.** If it's end-of-session, exit regardless of P&L.
2. **Stop exits run second.** If price hit the stop loss, exit (protect capital).
3. **Target exits run third.** If price hit the profit target, exit (capture gains).
4. **Trailing stop runs last.** If none of the above fired, check trailing stop.

## Consequences

### Positive
- **Capital preservation:** Hard stops are evaluated early, protecting against large losses even near market close.
- **Predictability:** Traders can reason about behavior: "If my stop and target both hit, stop wins."
- **Session discipline:** EOD takes precedence over all technical levels, enforcing overnight risk rules.
- **Trailing stop efficiency:** Only evaluated when other exits don't fire, avoiding redundant checks.

### Negative
- **Opportunity cost:** A trailing stop might have captured more profit than a fixed target if both trigger near each other. Acceptable trade-off for predictability.
- **Edge case complexity:** If stop and target are at the same price, stop wins. Documented and tested.

## Alternatives Considered

### Option A: Stop → Target → Trail → EOD
**Rejected:** Would allow positions to violate overnight risk rules if stop/target fired late in session.

### Option B: Evaluate all simultaneously, pick "best" exit
**Rejected:** Adds complexity. "Best" is ambiguous (highest P&L vs. risk-adjusted). Deterministic priority is simpler to debug.

### Option C: User-configurable priority
**Rejected:** Premature optimization. Fixed priority covers 99% of use cases. Can revisit if needed.

## Implementation Details

The priority is hardcoded in `_run_manage_positions_once()` in `trading_bot/cli/app.py`:

```python
# Priority order: EOD > Stop > Target > Trail
actions = 0
for pos in state.positions:
    exit_triggered = False

    # 1. EOD exit
    if eod_active and not exit_triggered:
        # ... exit logic ...
        exit_triggered = True

    # 2. Stop loss
    if pos.stop_loss and price <= pos.stop_loss and not exit_triggered:
        # ... exit logic ...
        exit_triggered = True

    # 3. Profit target
    if pos.profit_target and price >= pos.profit_target and not exit_triggered:
        # ... exit logic ...
        exit_triggered = True

    # 4. Trailing stop
    trail = next_trailing_stop(...)
    if trail and price <= trail and not exit_triggered:
        # ... exit logic ...
```

The `exit_triggered` flag ensures only one exit fires per position per iteration.

## Testing

Covered by:
- `test_manage_positions_executes_stop_exit`: Verifies stop fires before target
- `test_manage_positions_executes_eod_exit_before_other_exits`: Verifies EOD precedence
- `test_manage_positions_executes_target_exit`: Verifies target fires when stop doesn't
- `test_manage_positions_executes_trailing_stop`: Verifies trail fires when others don't

## Related Documents
- `V1_APP_OVERVIEW.md` (V2 section)
- `trading_bot/cli/app.py` (`_run_manage_positions_once()`)
- `trading_bot/runtime/session.py` (`should_eod_exit()`)

## Changelog
- 2026-06-18: Initial decision
- 2026-06-18: Fixed chandelier stop to use bar high instead of close price for `highest_high` tracking. This ensures wicks/spikes are captured, making the trailing stop materially stronger. See `test_manage_positions_chandelier_uses_bar_high_not_close`.

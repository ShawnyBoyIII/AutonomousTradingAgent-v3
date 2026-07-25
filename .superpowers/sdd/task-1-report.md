Status: DONE_WITH_CONCERNS
Branch: feature/runtime-canary-hardening
Base commits:
7628bd6 docs: plan runtime canary lifecycle hardening
9f19850 docs: design runtime canary lifecycle hardening
0fbf1ac feat(event_engine): add quantitative validation analytics
c98917e README: link to v3.0.0 release and document archived legacy repos
81641d1 fix(event_engine): replace unescaped backslash in strategy docstring
Head commits: pending task commit
Summary: Added backward-compatible durable runtime-canary metadata columns, atomic fill persistence, chronological experiment-row queries, and migration/round-trip tests.
Focused test: .venv/bin/python -m pytest tests/test_runtime_canary_durable_orders.py -q
Focused output: 2 passed in 3.27s
Regression test: .venv/bin/python -m pytest tests/test_runtime_canary_durable_orders.py tests/test_paper_broker.py tests/test_ledger_locks.py -q
Regression output: 1 failed, 24 passed in 3.27s
Concerns: tests/test_paper_broker.py::test_ledger_initializes_sqlite_tables asserts the obsolete exact nine-column orders schema; the mandated two additive columns make that assertion fail, and task scope explicitly forbids modifying the existing test.

## Fix: schema pin

Status: DONE

Line changed (tests/test_paper_broker.py:141):
- Before: `assert columns == ["id", "ticker", "side", "quantity", "fill_price", "fees", "filled_at", "pnl", "strategy_tag"]`
- After: full 11-column list preserving the original nine in order and appending `canary_experiment_id` and `canary_baseline_quantity` last (matches the additive migration in `trading_bot/portfolio/ledger.py:73-93`).

Test command: `.venv/bin/python -m pytest tests/test_runtime_canary_durable_orders.py tests/test_paper_broker.py tests/test_ledger_locks.py -q`
Result: 25 passed in 0.12s (all green).

Full-suite command: `.venv/bin/python -m pytest -q`
Result: 2101 passed, 1 failed in 28.11s. The single failure is `tests/test_backtest_runner.py::test_run_symbol_backtest_replays_multiple_trade_cycles` (`net_pnl: 53.0 != 45.4`) — the documented pre-existing failure. No other regressions.

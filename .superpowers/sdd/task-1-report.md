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

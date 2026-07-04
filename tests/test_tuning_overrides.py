from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from trading_bot.config.settings import Settings
from trading_bot.learning.tuning_overrides import propose_tuning_overrides
from trading_bot.strategy.strategy_tracker import record_exit


def test_propose_tuning_overrides_relaxes_supermodel_when_rejections_dominate(
    tmp_path: Path,
) -> None:
    settings = Settings()
    settings.app.log_dir = str(tmp_path / "logs")
    scan_results_path = tmp_path / "state" / "scan_results.json"
    scan_results_path.parent.mkdir(parents=True)
    scan_results_path.write_text(
        json.dumps(
            {
                "summary": {
                    "approved": 1,
                    "rejected": 9,
                }
            }
        ),
        encoding="utf-8",
    )

    for i in range(20):
        win = i < 6
        record_exit(
            Path(settings.app.log_dir),
            "v3-breakout",
            "AAPL",
            entry_price=100.0,
            exit_price=101.0 if win else 99.0,
            quantity=1,
            fees=1.0,
            pnl=10.0 if win else -10.0,
            reason="target" if win else "stop",
            timestamp=datetime(2026, 7, 1 + i),
        )

    proposal = propose_tuning_overrides(Path(settings.app.log_dir), settings, scan_results_path)

    assert proposal["supermodel"]["block_threshold"] == 0.25
    assert proposal["supermodel"]["counter_veto_weight"] == 0.75
    assert proposal["strategy_tracker"]["full_allocation_rate"] == 0.55

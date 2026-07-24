from __future__ import annotations

import inspect
import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from typer.testing import CliRunner

from trading_bot.config.settings import Settings
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.runtime.position_management import evaluate_exit_priority


NOW = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)


def _position(**updates) -> Position:
    values = {
        "ticker": "AAPL",
        "quantity": 10,
        "average_cost": 100.0,
        "stop_loss": 95.0,
        "profit_target": 110.0,
        "entry_at": NOW,
    }
    values.update(updates)
    return Position(**values)


@pytest.mark.parametrize(
    ("position", "price", "eod_active", "time_exit", "reason"),
    [
        (_position(), 100.0, True, 0, "eod_exit"),
        (_position(), 94.0, False, 0, "stop_loss"),
        (_position(), 111.0, False, 0, "profit_target"),
        (
            _position(stop_loss=None, profit_target=None, entry_at=NOW - timedelta(minutes=45)),
            100.0,
            False,
            30,
            "time_exit_45m",
        ),
    ],
)
def test_fixed_priority_checks_return_canonical_reasons(
    position: Position,
    price: float,
    eod_active: bool,
    time_exit: int,
    reason: str,
) -> None:
    settings = Settings()
    settings.session.time_exit_minutes = time_exit

    decision = evaluate_exit_priority(
        position=position,
        current_price=price,
        settings=settings,
        now=NOW,
        eod_active=eod_active,
    )

    assert decision.reason == reason


def test_higher_priority_match_does_not_run_lazy_checks() -> None:
    calls: list[str] = []

    decision = evaluate_exit_priority(
        position=_position(stop_loss=101.0, profit_target=99.0),
        current_price=100.0,
        settings=Settings(),
        now=NOW,
        eod_active=True,
        counter_thesis_check=lambda: calls.append("counter"),
        trailing_stop_check=lambda: calls.append("trailing"),
    )

    assert decision.reason == "eod_exit"
    assert calls == []


def test_profit_target_precedes_time_exit() -> None:
    settings = Settings()
    settings.session.time_exit_minutes = 30

    decision = evaluate_exit_priority(
        position=_position(
            profit_target=99.0,
            entry_at=NOW - timedelta(minutes=45),
        ),
        current_price=100.0,
        settings=settings,
        now=NOW,
        eod_active=False,
    )

    assert decision.reason == "profit_target"


def test_time_exit_precedes_lazy_checks() -> None:
    settings = Settings()
    settings.session.time_exit_minutes = 30
    calls: list[str] = []

    decision = evaluate_exit_priority(
        position=_position(
            stop_loss=None,
            profit_target=None,
            entry_at=NOW - timedelta(minutes=45),
        ),
        current_price=100.0,
        settings=settings,
        now=NOW,
        eod_active=False,
        counter_thesis_check=lambda: calls.append("counter"),
        trailing_stop_check=lambda: calls.append("trailing"),
    )

    assert decision.reason == "time_exit_45m"
    assert calls == []


def test_counter_thesis_precedes_trailing_stop_and_carries_payload() -> None:
    counter_result = object()
    calls: list[str] = []

    decision = evaluate_exit_priority(
        position=_position(stop_loss=None, profit_target=None),
        current_price=100.0,
        settings=Settings(),
        now=NOW,
        eod_active=False,
        counter_thesis_check=lambda: calls.append("counter") or counter_result,
        trailing_stop_check=lambda: calls.append("trailing") or object(),
    )

    assert decision.reason == "counter_thesis"
    assert decision.payload is counter_result
    assert calls == ["counter"]


def test_trailing_stop_is_last_and_carries_payload() -> None:
    trailing_result = object()
    calls: list[str] = []

    decision = evaluate_exit_priority(
        position=_position(stop_loss=None, profit_target=None),
        current_price=100.0,
        settings=Settings(),
        now=NOW,
        eod_active=False,
        counter_thesis_check=lambda: calls.append("counter"),
        trailing_stop_check=lambda: calls.append("trailing") or trailing_result,
    )

    assert decision.reason == "trailing_stop"
    assert decision.payload is trailing_result
    assert calls == ["counter", "trailing"]


def test_partial_target_selection_is_part_of_canonical_decision() -> None:
    settings = Settings()
    settings.paper.partial_take_profit_enabled = True
    settings.paper.partial_take_profit_min_qty = 10

    decision = evaluate_exit_priority(
        position=_position(quantity=10),
        current_price=111.0,
        settings=settings,
        now=NOW,
        eod_active=False,
    )

    assert decision.reason == "profit_target"
    assert decision.partial is True


def test_both_production_callers_call_shared_evaluator() -> None:
    from trading_bot.cli.app import _run_manage_positions_once as cli_manage
    from trading_bot.runtime.continuous_loop import (
        _run_manage_positions_once as continuous_manage,
    )

    assert "evaluate_exit_priority(" in inspect.getsource(cli_manage)
    assert "evaluate_exit_priority(" in inspect.getsource(continuous_manage)


def test_cli_fill_wrapper_separates_outward_and_canonical_reasons(
    monkeypatch,
) -> None:
    app_module = importlib.import_module("trading_bot.cli.app")

    captured: dict[str, object] = {}

    def _capture_fill(**kwargs):
        captured.update(kwargs)
        return "state", "event", "line"

    monkeypatch.setattr(app_module, "_shared_fill_sell_position", _capture_fill)

    result = app_module._fill_sell_position(
        ticker="AAPL",
        position=_position(),
        reason="eod",
        exit_reason="eod_exit",
        submitted_at=NOW,
        last_price=100.0,
        broker=object(),
        ledger=object(),
        state=object(),
        log_path=Path("decision-log.jsonl"),
        bars=pd.DataFrame(),
        settings=Settings(),
    )

    assert result == ("state", "event", "line")
    assert captured["reason"] == "eod"
    assert captured["exit_reason"] == "eod_exit"


def test_cli_partial_exit_keeps_target_partial_event_reason(
    monkeypatch, tmp_path: Path
) -> None:
    import trading_bot.data.market_data as market_data

    app_module = importlib.import_module("trading_bot.cli.app")

    monkeypatch.setattr(
        app_module,
        "now_in_zone",
        lambda timezone_name: datetime(
            2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone_name)
        ),
    )
    monkeypatch.setattr(
        market_data,
        "fetch_bars",
        lambda *args, **kwargs: pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-06-18T09:55:00"]),
                "close": [110.0],
            }
        ),
    )

    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n"
        "paper:\n"
        "  partial_take_profit_enabled: true\n"
        "  partial_take_profit_fraction: 0.5\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": _position(
                    stop_loss=98.0,
                    profit_target=108.0,
                )
            },
        )
    )

    result = CliRunner().invoke(
        app_module.app,
        ["--config-path", str(config_path), "manage-positions"],
    )

    assert result.exit_code == 0
    event = json.loads(
        (log_dir / "decision-log.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert event["reason"] == "target_partial"

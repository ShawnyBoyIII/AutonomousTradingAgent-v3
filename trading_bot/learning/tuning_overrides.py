from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from trading_bot.config.settings import Settings
from trading_bot.strategy.strategy_tracker import strategy_summary


def propose_tuning_overrides(
    log_dir: Path,
    settings: Settings,
    scan_results_path: Path | None = None,
) -> dict[str, dict[str, float | int]]:
    proposal: dict[str, dict[str, float | int]] = {
        "supermodel": {
            "support_threshold": settings.supermodel.support_threshold,
            "block_threshold": settings.supermodel.block_threshold,
            "counter_veto_weight": settings.supermodel.counter_veto_weight,
        },
        "strategy_tracker": {
            "window": settings.strategy_tracker.window,
            "min_win_rate": settings.strategy_tracker.min_win_rate,
            "full_allocation_rate": settings.strategy_tracker.full_allocation_rate,
        },
    }

    approved, rejected = _scan_counts(scan_results_path)
    total_reviewed = approved + rejected
    if total_reviewed > 0:
        rejection_rate = rejected / total_reviewed
        if rejection_rate > 0.6 and approved < 3:
            proposal["supermodel"]["block_threshold"] = round(
                max(0.15, settings.supermodel.block_threshold - 0.05),
                2,
            )
            proposal["supermodel"]["counter_veto_weight"] = round(
                max(0.0, settings.supermodel.counter_veto_weight - 0.25),
                2,
            )

    rows = strategy_summary(log_dir, window=settings.strategy_tracker.window)
    recent_exits = sum(int(row["recent_exits"]) for row in rows)
    recent_wins = sum(int(row["recent_wins"]) for row in rows)
    if recent_exits >= settings.strategy_tracker.window:
        recent_win_rate = recent_wins / recent_exits if recent_exits else 0.0
        if recent_win_rate < settings.strategy_tracker.full_allocation_rate:
            proposal["strategy_tracker"]["full_allocation_rate"] = round(
                min(0.75, settings.strategy_tracker.full_allocation_rate + 0.05),
                2,
            )

    # Data-store nudge: if recent bars in the long-term store show realised
    # volatility spiking above what the strategy_tracker is sized for, raise
    # the window so we look at more trades before changing sizing. This is
    # intentionally conservative — the long-term store is opt-in and may be
    # empty on first runs.
    _maybe_nudge_window_from_data_store(settings, proposal)

    return proposal


def _maybe_nudge_window_from_data_store(
    settings: Settings,
    proposal: dict[str, dict[str, float | int]],
) -> None:
    """If the EOD data store has recent bars, use them to nudge the window.

    Reads daily bars for the last 30 days and computes a coarse realised
    volatility proxy (mean |daily return|). High volatility suggests more
    trades are needed to draw a conclusion — nudge ``window`` up.
    """
    try:
        from trading_bot.data.data_store import (
            DataStoreManifest,
            read_bars,
        )

        cfg = settings.eod_data_store
        if not cfg.enabled:
            return
        root = Path(cfg.store_root)
        manifest = DataStoreManifest(db_path=Path(cfg.manifest_db))
        symbol = manifest.first_symbol()
        if not symbol:
            return
        end = date.today()
        start = end - timedelta(days=30)
        df = read_bars(symbol, "1d", start, end, root)
        if df.empty or len(df) < 5:
            return
        avg_abs_return = _avg_abs_return(df)
        # Threshold tuned empirically: > 3% daily move is "elevated".
        if avg_abs_return > 0.03:
            proposal["strategy_tracker"]["window"] = min(
                settings.strategy_tracker.window + 5, 60
            )
    except Exception:  # noqa: BLE001
        # Data store is opt-in; never let it break the override proposal.
        return


def _avg_abs_return(df) -> float:
    """Mean of |close-to-close return| for the rows in df."""
    if df.empty or "close" not in df.columns:
        return 0.0
    closes = df["close"].astype(float)
    if len(closes) < 2:
        return 0.0
    returns = closes.pct_change().dropna().abs()
    if returns.empty:
        return 0.0
    return float(returns.mean())


def write_tuning_overrides(path: Path, proposal: dict[str, dict[str, float | int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        yaml.safe_dump(proposal, handle, sort_keys=False)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _scan_counts(scan_results_path: Path | None) -> tuple[int, int]:
    if scan_results_path is None or not scan_results_path.exists():
        return 0, 0
    try:
        payload = yaml.safe_load(scan_results_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return 0, 0
    if not isinstance(payload, dict):
        return 0, 0
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return 0, 0
    return _as_int(summary.get("approved")), _as_int(summary.get("rejected"))


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

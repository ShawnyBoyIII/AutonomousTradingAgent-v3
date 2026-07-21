from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from trading_bot.config.settings import Settings
from trading_bot.strategy.strategy_tracker import strategy_summary


# Reducer block: defaults chosen for first-step safety. The offline replay
# gate in Plan B still gates every candidate; this hook only determines
# whether the *proposal* should surface the trend multiplier change.
TREND_REDUCER_STRATEGY_TAG = "v3-trend_following"
TREND_REDUCER_FIRST_STEP = 0.5
TREND_REDUCER_MIN_SELL_TRADES = 15
TREND_REDUCER_MAX_WIN_RATE = 0.50
TREND_REDUCER_EVIDENCE_LOOKBACK_HOURS = 36
TREND_REDUCER_MIN_AVG_PNL = 0.0


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
            "range_bound_trend_caution_multiplier": (
                settings.supermodel.range_bound_trend_caution_multiplier
            ),
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

    _maybe_apply_trend_reducer(settings, proposal)

    # Data-store nudge: if recent bars in the long-term store show realised
    # volatility spiking above what the strategy_tracker is sized for, raise
    # the window so we look at more trades before changing sizing. This is
    # intentionally conservative — the long-term store is opt-in and may be
    # empty on first runs.
    _maybe_nudge_window_from_data_store(settings, proposal)

    return proposal


def _maybe_apply_trend_reducer(
    settings: Settings,
    proposal: dict[str, dict[str, float | int]],
) -> None:
    """Surface a multiplier reduction when the cohort evidence supports it.

    The proposal still has to clear the offline replay + canary gates in
    Plan B; this hook merely lets the nightly tuner *offer* a candidate.
    It is intentionally additive: it never raises the multiplier, only
    proposes a single discrete first-step reduction (1.0 → 0.5).
    """
    current = float(settings.supermodel.range_bound_trend_caution_multiplier)
    if current <= TREND_REDUCER_FIRST_STEP:
        # Already at or below the first step; don't keep halving.
        return

    evidence = _trend_evidence_lookup(settings)
    if evidence is None:
        return

    n_sells, win_rate, avg_pnl = evidence
    if n_sells < TREND_REDUCER_MIN_SELL_TRADES:
        return
    if win_rate > TREND_REDUCER_MAX_WIN_RATE:
        return
    if avg_pnl >= TREND_REDUCER_MIN_AVG_PNL:
        return

    proposal["supermodel"]["range_bound_trend_caution_multiplier"] = (
        TREND_REDUCER_FIRST_STEP
    )


def _trend_evidence_lookup(
    settings: Settings,
) -> tuple[int, float, float] | None:
    """Return (n_sells, win_rate, avg_pnl) for the targeted bucket.

    Looks at the cohort's persisted SELL rows for the targeted strategy tag.
    Returns None if the state DB is missing or unreadable so the reducer
    never breaks the override proposal pipeline.
    """
    db_path = Path(settings.app.state_db_path)
    if not db_path.exists():
        return None
    try:
        import sqlite3

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=TREND_REDUCER_EVIDENCE_LOOKBACK_HOURS)
        ).isoformat()
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS n_sells,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS n_wins,
                    AVG(pnl) AS avg_pnl
                FROM orders
                WHERE side = 'SELL'
                  AND strategy_tag = ?
                  AND filled_at >= ?
                """,
                (TREND_REDUCER_STRATEGY_TAG, cutoff),
            ).fetchone()
        if row is None or row[0] == 0:
            return None
        n_sells = int(row[0])
        n_wins = int(row[1] or 0)
        avg_pnl = float(row[2] or 0.0)
        win_rate = n_wins / n_sells if n_sells else 0.0
        return n_sells, win_rate, avg_pnl
    except Exception:  # noqa: BLE001
        return None


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

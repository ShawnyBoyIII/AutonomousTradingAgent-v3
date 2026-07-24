"""Statistical validation and overfitting diagnostics for backtests.

The module consumes ordinary pandas objects so it can analyse an
``EngineDriver`` trace, a broker export, or a synthetic experiment without
depending on the live trading application. Sharpe values used by PSR and DSR
are per-observation values; callers should supply trial Sharpes on the same
scale. Performance-report ratios are annualized with ``periods_per_year``.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, permutations
from math import ceil, log, sqrt
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew, ttest_1samp


SIGNIFICANCE_LEVEL = 0.05
_EULER_MASCHERONI = 0.5772156649015329


@dataclass(frozen=True, slots=True)
class SQNResult:
    sqn_100: float
    rating: str
    trade_count: int
    mean_r: float
    std_r: float
    p_value: float
    significant: bool


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    cagr: float
    annualized_volatility: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown: float
    max_drawdown_duration: int
    observation_count: int


@dataclass(frozen=True, slots=True)
class SharpeProbabilityResult:
    probability: float
    p_value: float
    significant: bool
    observed_sharpe: float
    benchmark_sharpe: float
    sample_length: int
    skewness: float
    kurtosis: float


@dataclass(frozen=True, slots=True)
class DeflatedSharpeResult(SharpeProbabilityResult):
    trial_count: int
    trial_sharpe_variance: float


@dataclass(frozen=True, slots=True)
class PurgedSplit:
    train_indices: np.ndarray
    test_indices: np.ndarray
    test_groups: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CPCVResult:
    n_splits: int
    pbo: float
    p_value: float
    significant: bool
    p_value_method: str
    selected_strategies: tuple[str, ...]
    oos_scores: tuple[float, ...]
    oos_rank_logits: tuple[float, ...]
    path_returns: tuple[pd.Series, ...]


class PerformanceAnalytics:
    """Calculate risk-adjusted metrics from equity and closed trades.

    ``trades`` is optional. When supplied it must include ``pnl`` and
    ``initial_risk`` columns. Zero initial risk is undefined and is retained as
    ``NaN`` in the R-multiple log, then excluded from SQN.
    """

    def __init__(
        self,
        equity_curve: pd.Series,
        trades: pd.DataFrame | None = None,
        *,
        periods_per_year: int = 252,
        risk_free_rate: float = 0.0,
    ) -> None:
        if not isinstance(equity_curve.index, pd.DatetimeIndex):
            raise ValueError("equity_curve must use a DatetimeIndex")
        if not equity_curve.index.is_monotonic_increasing:
            raise ValueError("equity_curve must be chronological")
        if equity_curve.index.has_duplicates:
            raise ValueError("equity_curve timestamps must be unique")
        values = equity_curve.to_numpy(dtype=np.float64)
        if len(values) < 2:
            raise ValueError("equity_curve requires at least two observations")
        if not np.isfinite(values).all():
            raise ValueError("equity_curve values must be finite")
        if np.any(values <= 0.0):
            raise ValueError("equity_curve values must be positive")
        if periods_per_year <= 0:
            raise ValueError("periods_per_year must be positive")
        if trades is not None and not {"pnl", "initial_risk"}.issubset(trades.columns):
            raise ValueError("trades must contain pnl and initial_risk columns")

        self.equity_curve = equity_curve.astype("float64").copy()
        self.trades = trades.copy() if trades is not None else pd.DataFrame(
            columns=["pnl", "initial_risk"]
        )
        self.periods_per_year = int(periods_per_year)
        self.risk_free_rate = float(risk_free_rate)

    @property
    def returns(self) -> pd.Series:
        return self.equity_curve.pct_change().dropna()

    def r_multiples(self) -> pd.Series:
        pnl = pd.to_numeric(self.trades["pnl"], errors="coerce")
        risk = pd.to_numeric(self.trades["initial_risk"], errors="coerce")
        valid_risk = risk.where(risk > 0.0)
        result = (pnl / valid_risk).astype("float64")
        result.name = "r_multiple"
        return result

    def sqn(self) -> SQNResult:
        values = self.r_multiples().dropna().to_numpy(dtype=np.float64)
        values = values[np.isfinite(values)]
        count = len(values)
        if count == 0:
            return SQNResult(0.0, "Insufficient Evidence", 0, 0.0, 0.0, 1.0, False)
        mean_r = float(np.mean(values))
        std_r = float(np.std(values, ddof=1)) if count > 1 else 0.0
        if std_r == 0.0:
            sqn = float("inf") if mean_r > 0.0 else 0.0
            p_value = 0.0 if mean_r > 0.0 and count > 1 else 1.0
        else:
            sqn = sqrt(min(count, 100)) * mean_r / std_r
            test = ttest_1samp(values, popmean=0.0, alternative="greater")
            p_value = float(test.pvalue) if np.isfinite(test.pvalue) else 1.0
        return SQNResult(
            sqn_100=float(sqn),
            rating=self.tharp_rating(sqn),
            trade_count=count,
            mean_r=mean_r,
            std_r=std_r,
            p_value=p_value,
            significant=p_value < SIGNIFICANCE_LEVEL,
        )

    @staticmethod
    def tharp_rating(sqn: float) -> str:
        if sqn < 1.6:
            return "Poor"
        if sqn < 2.0:
            return "Below Average"
        if sqn < 2.5:
            return "Average"
        if sqn < 3.0:
            return "Good"
        if sqn < 5.1:
            return "Excellent"
        if sqn < 7.0:
            return "Superb"
        return "Holy Grail"

    def metrics(self) -> PerformanceMetrics:
        returns = self.returns.to_numpy(dtype=np.float64)
        elapsed_years = (
            (self.equity_curve.index[-1] - self.equity_curve.index[0]).total_seconds()
            / (365.25 * 24.0 * 60.0 * 60.0)
        )
        total_growth = float(self.equity_curve.iloc[-1] / self.equity_curve.iloc[0])
        cagr = total_growth ** (1.0 / elapsed_years) - 1.0 if elapsed_years > 0.0 else 0.0
        volatility = (
            float(np.std(returns, ddof=1) * sqrt(self.periods_per_year))
            if len(returns) > 1
            else 0.0
        )
        annual_excess = float(np.mean(returns) * self.periods_per_year) - self.risk_free_rate
        target_per_period = self.risk_free_rate / self.periods_per_year
        downside = np.minimum(returns - target_per_period, 0.0)
        downside_deviation = float(
            sqrt(np.mean(np.square(downside))) * sqrt(self.periods_per_year)
        )
        if downside_deviation > 0.0:
            sortino = annual_excess / downside_deviation
        elif annual_excess > 0.0:
            sortino = float("inf")
        elif annual_excess < 0.0:
            sortino = float("-inf")
        else:
            sortino = 0.0

        max_drawdown, duration = self._drawdown_statistics()
        if max_drawdown > 0.0:
            calmar = cagr / max_drawdown
        elif cagr > 0.0:
            calmar = float("inf")
        else:
            calmar = 0.0
        return PerformanceMetrics(
            cagr=float(cagr),
            annualized_volatility=volatility,
            sortino_ratio=float(sortino),
            calmar_ratio=float(calmar),
            max_drawdown=max_drawdown,
            max_drawdown_duration=duration,
            observation_count=len(self.equity_curve),
        )

    def _drawdown_statistics(self) -> tuple[float, int]:
        values = self.equity_curve.to_numpy(dtype=np.float64)
        peaks = np.maximum.accumulate(values)
        drawdowns = 1.0 - values / peaks
        max_drawdown = float(np.max(drawdowns))

        peak_position = 0
        max_duration = 0
        in_drawdown = False
        for position, (value, peak) in enumerate(zip(values, peaks)):
            if value >= peak:
                if in_drawdown:
                    max_duration = max(max_duration, position - peak_position)
                peak_position = position
                in_drawdown = False
            else:
                in_drawdown = True
        if in_drawdown:
            max_duration = max(max_duration, len(values) - 1 - peak_position)
        return max_drawdown, int(max_duration)


class DSRDiagnostics:
    """López de Prado PSR and multiple-testing-adjusted DSR."""

    @staticmethod
    def probabilistic_sharpe_ratio(
        returns: pd.Series | Sequence[float],
        *,
        benchmark_sharpe: float = 0.0,
    ) -> SharpeProbabilityResult:
        values = _validated_returns(returns)
        observed, sample_skew, sample_kurtosis, zero_variance = _return_moments(values)
        probability = _psr_probability(
            observed,
            benchmark_sharpe,
            len(values),
            sample_skew,
            sample_kurtosis,
            zero_variance=zero_variance,
        )
        p_value = 1.0 - probability
        return SharpeProbabilityResult(
            probability=probability,
            p_value=p_value,
            significant=p_value < SIGNIFICANCE_LEVEL,
            observed_sharpe=observed,
            benchmark_sharpe=float(benchmark_sharpe),
            sample_length=len(values),
            skewness=sample_skew,
            kurtosis=sample_kurtosis,
        )

    @staticmethod
    def deflated_sharpe_ratio(
        returns: pd.Series | Sequence[float],
        *,
        trial_sharpes: Iterable[float],
        n_trials: int | None = None,
    ) -> DeflatedSharpeResult:
        values = _validated_returns(returns)
        trials = np.asarray(tuple(trial_sharpes), dtype=np.float64)
        if len(trials) < 2 or not np.isfinite(trials).all():
            raise ValueError("trial_sharpes requires at least two finite values")
        total_trials = int(n_trials if n_trials is not None else len(trials))
        if total_trials < 1:
            raise ValueError("n_trials must be positive")
        trial_variance = float(np.var(trials, ddof=1))
        benchmark = _expected_maximum_sharpe(total_trials, trial_variance)
        observed, sample_skew, sample_kurtosis, zero_variance = _return_moments(values)
        probability = _psr_probability(
            observed,
            benchmark,
            len(values),
            sample_skew,
            sample_kurtosis,
            zero_variance=zero_variance,
        )
        p_value = 1.0 - probability
        return DeflatedSharpeResult(
            probability=probability,
            p_value=p_value,
            significant=p_value < SIGNIFICANCE_LEVEL,
            observed_sharpe=observed,
            benchmark_sharpe=benchmark,
            sample_length=len(values),
            skewness=sample_skew,
            kurtosis=sample_kurtosis,
            trial_count=total_trials,
            trial_sharpe_variance=trial_variance,
        )


def _validated_returns(returns: pd.Series | Sequence[float]) -> np.ndarray:
    values = np.asarray(returns, dtype=np.float64)
    if values.ndim != 1 or len(values) < 3:
        raise ValueError("returns must contain at least three observations")
    if not np.isfinite(values).all():
        raise ValueError("returns must be finite")
    return values


def _return_moments(values: np.ndarray) -> tuple[float, float, float, bool]:
    standard_deviation = float(np.std(values, ddof=1))
    scale = max(1.0, float(np.max(np.abs(values))))
    zero_variance = standard_deviation <= np.finfo(np.float64).eps * scale * 10.0
    if zero_variance:
        sample_mean = float(np.mean(values))
        observed = float("inf") if sample_mean > 0.0 else (
            float("-inf") if sample_mean < 0.0 else 0.0
        )
        return observed, 0.0, 3.0, True
    observed = float(np.mean(values) / standard_deviation)
    sample_skew = float(skew(values, bias=False))
    sample_kurtosis = float(kurtosis(values, fisher=False, bias=False))
    return observed, sample_skew, sample_kurtosis, False


def _psr_probability(
    observed: float,
    benchmark: float,
    sample_length: int,
    sample_skew: float,
    sample_kurtosis: float,
    *,
    zero_variance: bool,
) -> float:
    if zero_variance:
        return 0.5 if observed == benchmark else float(observed > benchmark)
    correction = 1.0 - sample_skew * observed + (
        (sample_kurtosis - 1.0) * observed * observed / 4.0
    )
    if correction <= 0.0 or not np.isfinite(correction):
        return 0.5
    statistic = (observed - benchmark) * sqrt(sample_length - 1) / sqrt(correction)
    return float(np.clip(norm.cdf(statistic), 0.0, 1.0))


def _expected_maximum_sharpe(n_trials: int, trial_variance: float) -> float:
    if n_trials <= 1 or trial_variance <= 0.0:
        return 0.0
    first = norm.ppf(1.0 - 1.0 / n_trials)
    second = norm.ppf(1.0 - 1.0 / (n_trials * np.e))
    expected_standard_max = (
        (1.0 - _EULER_MASCHERONI) * first
        + _EULER_MASCHERONI * second
    )
    return float(sqrt(trial_variance) * expected_standard_max)


def purge_overlapping_events(
    event_starts: pd.DatetimeIndex,
    event_ends: pd.Series | Sequence[pd.Timestamp],
    train_indices: Sequence[int],
    test_indices: Sequence[int],
) -> np.ndarray:
    """Drop train events whose closed intervals overlap any test event."""
    starts, ends = _validated_event_windows(event_starts, event_ends)
    train = np.asarray(train_indices, dtype=np.int64)
    test = np.asarray(test_indices, dtype=np.int64)
    if not len(train) or not len(test):
        return train.copy()
    test_intervals = sorted(
        zip(starts.take(test).asi8.tolist(), ends.take(test).asi8.tolist())
    )
    merged_starts: list[int] = []
    merged_ends: list[int] = []
    for interval_start, interval_end in test_intervals:
        if merged_ends and interval_start <= merged_ends[-1]:
            merged_ends[-1] = max(merged_ends[-1], interval_end)
        else:
            merged_starts.append(interval_start)
            merged_ends.append(interval_end)
    merged_start_array = np.asarray(merged_starts, dtype=np.int64)
    merged_end_array = np.asarray(merged_ends, dtype=np.int64)
    train_starts = starts.take(train).asi8
    train_ends = ends.take(train).asi8
    candidate = np.searchsorted(merged_start_array, train_ends, side="right") - 1
    overlaps = candidate >= 0
    overlaps[overlaps] &= merged_end_array[candidate[overlaps]] >= train_starts[overlaps]
    return train[~overlaps]


def apply_embargo(
    train_indices: Sequence[int],
    test_indices: Sequence[int],
    *,
    n_samples: int,
    embargo_pct: float,
    event_starts: pd.DatetimeIndex | None = None,
    event_ends: pd.Series | Sequence[pd.Timestamp] | None = None,
) -> np.ndarray:
    """Remove a post-test autocorrelation buffer from training indices."""
    if not 0.0 <= embargo_pct < 1.0:
        raise ValueError("embargo_pct must be in [0, 1)")
    train = np.asarray(train_indices, dtype=np.int64)
    test = np.sort(np.asarray(test_indices, dtype=np.int64))
    embargo_size = int(ceil(n_samples * embargo_pct))
    if not len(test) or embargo_size == 0:
        return train.copy()
    blocked: set[int] = set()
    if (event_starts is None) != (event_ends is None):
        raise ValueError("event_starts and event_ends must be supplied together")
    starts_and_ends = (
        _validated_event_windows(event_starts, event_ends)
        if event_starts is not None and event_ends is not None
        else None
    )

    group_start = int(test[0])
    group_end = int(test[0])

    def block_after_horizon(start_position: int, end_position: int) -> None:
        embargo_anchor = end_position
        if starts_and_ends is not None:
            starts, ends = starts_and_ends
            horizon = ends.take(test[(test >= start_position) & (test <= end_position)]).max()
            embargo_anchor = int(starts.searchsorted(horizon, side="right") - 1)
        blocked.update(
            range(
                embargo_anchor + 1,
                min(embargo_anchor + embargo_size + 1, n_samples),
            )
        )

    for previous, current in zip(test, test[1:]):
        if int(current) != int(previous) + 1:
            block_after_horizon(group_start, group_end)
            group_start = int(current)
        group_end = int(current)
    block_after_horizon(group_start, group_end)
    return train[~np.isin(train, np.fromiter(blocked, dtype=np.int64))]


class CombinatorialPurgedCV:
    """CPCV splitter and Probability of Backtest Overfitting estimator."""

    def __init__(
        self,
        n_groups: int = 6,
        n_test_groups: int = 2,
        *,
        embargo_pct: float = 0.05,
    ) -> None:
        if n_groups < 2:
            raise ValueError("n_groups must be at least two")
        if not 0 < n_test_groups < n_groups:
            raise ValueError("n_test_groups must be between zero and n_groups")
        if not 0.0 <= embargo_pct < 1.0:
            raise ValueError("embargo_pct must be in [0, 1)")
        self.n_groups = int(n_groups)
        self.n_test_groups = int(n_test_groups)
        self.embargo_pct = float(embargo_pct)

    def split(
        self,
        event_starts: pd.DatetimeIndex,
        event_ends: pd.Series | Sequence[pd.Timestamp],
    ) -> Iterator[PurgedSplit]:
        starts, ends = _validated_event_windows(event_starts, event_ends)
        if len(starts) < self.n_groups:
            raise ValueError("event count must be at least n_groups")
        groups = tuple(np.asarray(group, dtype=np.int64) for group in np.array_split(
            np.arange(len(starts), dtype=np.int64), self.n_groups
        ))
        for selected in combinations(range(self.n_groups), self.n_test_groups):
            test = np.sort(np.concatenate([groups[group] for group in selected]))
            train_groups = [
                groups[group] for group in range(self.n_groups) if group not in selected
            ]
            train = np.sort(np.concatenate(train_groups))
            train = purge_overlapping_events(starts, ends, train, test)
            train = apply_embargo(
                train,
                test,
                n_samples=len(starts),
                embargo_pct=self.embargo_pct,
                event_starts=starts,
                event_ends=ends,
            )
            yield PurgedSplit(train, test, tuple(selected))

    def evaluate(
        self,
        trial_returns: pd.DataFrame,
        *,
        event_ends: pd.Series | Sequence[pd.Timestamp],
    ) -> CPCVResult:
        if not isinstance(trial_returns.index, pd.DatetimeIndex):
            raise ValueError("trial_returns must use a DatetimeIndex")
        if trial_returns.shape[1] < 2:
            raise ValueError("CPCV requires at least two strategy trials")
        values = trial_returns.to_numpy(dtype=np.float64)
        if not np.isfinite(values).all():
            raise ValueError("trial_returns must be finite")

        selected: list[str] = []
        selected_positions: list[int] = []
        scores: list[float] = []
        logits: list[float] = []
        oos_score_rows: list[np.ndarray] = []
        evaluated_splits: list[PurgedSplit] = []
        for split in self.split(trial_returns.index, event_ends):
            if len(split.train_indices) < 2:
                raise ValueError(
                    "CPCV requires at least two training observations after purging and embargo"
                )
            train_scores = _column_sharpes(values[split.train_indices, :])
            selected_position = int(np.argmax(train_scores))
            selected_name = str(trial_returns.columns[selected_position])
            if len(split.test_indices) < 2:
                raise ValueError(
                    "CPCV requires at least two test observations per split"
                )
            test_values = values[split.test_indices, :]
            test_scores = _column_sharpes(test_values)
            rank = _normalized_rank(test_scores, selected_position)
            clipped_rank = float(np.clip(rank, 1e-12, 1.0 - 1e-12))
            selected.append(selected_name)
            selected_positions.append(selected_position)
            scores.append(float(test_scores[selected_position]))
            logits.append(log(clipped_rank / (1.0 - clipped_rank)))
            oos_score_rows.append(test_scores)
            evaluated_splits.append(split)

        if not logits:
            raise ValueError("CPCV produced no viable training splits")
        overfit_count = sum(value <= 0.0 for value in logits)
        pbo = overfit_count / len(logits)
        p_value = _strategy_label_permutation_p_value(
            np.vstack(oos_score_rows),
            np.asarray(selected_positions, dtype=np.int64),
            observed_overfit_count=overfit_count,
        )
        paths = _assemble_cpcv_paths(
            trial_returns,
            evaluated_splits,
            selected_positions,
            n_groups=self.n_groups,
        )
        return CPCVResult(
            n_splits=len(logits),
            pbo=float(pbo),
            p_value=p_value,
            significant=p_value < SIGNIFICANCE_LEVEL,
            p_value_method="strategy-label permutation",
            selected_strategies=tuple(selected),
            oos_scores=tuple(scores),
            oos_rank_logits=tuple(logits),
            path_returns=tuple(paths),
        )


def _normalized_rank(scores: np.ndarray, selected_position: int) -> float:
    """Return the PBO relative rank ``r / (M + 1)`` (higher is better)."""
    ranks = pd.Series(scores).rank(method="average").to_numpy(dtype=np.float64)
    return float(ranks[selected_position] / (len(scores) + 1.0))


def _strategy_label_permutation_p_value(
    oos_scores: np.ndarray,
    selected_positions: np.ndarray,
    *,
    observed_overfit_count: int,
    monte_carlo_draws: int = 10_000,
) -> float:
    """Randomization test preserving dependence between overlapping splits."""
    n_splits, n_trials = oos_scores.shape
    rank_matrix = np.vstack(
        [
            pd.Series(row).rank(method="average").to_numpy(dtype=np.float64)
            / (n_trials + 1.0)
            for row in oos_scores
        ]
    )
    row_positions = np.arange(n_splits)

    def overfit_count(permutation: np.ndarray) -> int:
        permuted_positions = permutation[selected_positions]
        return int(np.sum(rank_matrix[row_positions, permuted_positions] <= 0.5))

    if n_trials <= 7:
        counts = [
            overfit_count(np.asarray(permutation, dtype=np.int64))
            for permutation in permutations(range(n_trials))
        ]
        return float(np.mean(np.asarray(counts) >= observed_overfit_count))

    rng = np.random.default_rng(0)
    exceedances = 0
    for _ in range(monte_carlo_draws):
        permutation = rng.permutation(n_trials)
        exceedances += overfit_count(permutation) >= observed_overfit_count
    return float((exceedances + 1) / (monte_carlo_draws + 1))


def _assemble_cpcv_paths(
    trial_returns: pd.DataFrame,
    splits: Sequence[PurgedSplit],
    selected_positions: Sequence[int],
    *,
    n_groups: int,
) -> list[pd.Series]:
    groups = tuple(
        np.asarray(group, dtype=np.int64)
        for group in np.array_split(np.arange(len(trial_returns)), n_groups)
    )
    path_count = sum(len(split.test_groups) for split in splits) // n_groups
    path_parts: list[list[pd.Series]] = [[] for _ in range(path_count)]
    for group_number, group_indices in enumerate(groups):
        containing_splits = [
            split_position
            for split_position, split in enumerate(splits)
            if group_number in split.test_groups
        ]
        if len(containing_splits) != path_count:
            raise ValueError("CPCV split topology cannot form complete paths")
        for path_number, split_position in enumerate(containing_splits):
            strategy_position = selected_positions[split_position]
            path_parts[path_number].append(
                trial_returns.iloc[group_indices, strategy_position].copy()
            )
    paths = [pd.concat(parts).sort_index() for parts in path_parts]
    if any(not path.index.equals(trial_returns.index) for path in paths):
        raise ValueError("CPCV path assembly did not cover the full horizon")
    return paths


def _validated_event_windows(
    event_starts: pd.DatetimeIndex,
    event_ends: pd.Series | Sequence[pd.Timestamp],
) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    if not isinstance(event_starts, pd.DatetimeIndex):
        raise ValueError("event_starts must be a DatetimeIndex")
    ends = pd.DatetimeIndex(pd.to_datetime(event_ends))
    if len(event_starts) != len(ends):
        raise ValueError("event_starts and event_ends must have equal length")
    if not event_starts.is_monotonic_increasing:
        raise ValueError("event_starts must be chronological")
    if np.any(ends.asi8 < event_starts.asi8):
        raise ValueError("event ends cannot precede starts")
    return event_starts, ends


def _column_sharpes(values: np.ndarray) -> np.ndarray:
    means = np.mean(values, axis=0)
    standard_deviations = np.std(values, axis=0, ddof=1)
    scales = np.maximum(1.0, np.max(np.abs(values), axis=0))
    near_zero = standard_deviations <= np.finfo(np.float64).eps * scales * 10.0
    sharpes = np.divide(
        means,
        standard_deviations,
        out=np.zeros_like(means),
        where=~near_zero,
    )
    sharpes[near_zero & (means > 0.0)] = np.inf
    sharpes[near_zero & (means < 0.0)] = -np.inf
    return sharpes


def generate_markdown_summary(
    analytics: PerformanceAnalytics,
    *,
    dsr_result: DeflatedSharpeResult | None = None,
    cpcv_result: CPCVResult | None = None,
) -> str:
    """Render a stable Markdown report with statistical disclosures."""
    metrics = analytics.metrics()
    sqn = analytics.sqn()
    lines = [
        "# Quantitative Performance Summary",
        "",
        "## Risk and Return",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| CAGR | {metrics.cagr:.2%} |",
        f"| Annualized Volatility | {metrics.annualized_volatility:.2%} |",
        f"| Sortino Ratio | {_format_number(metrics.sortino_ratio)} |",
        f"| Calmar Ratio | {_format_number(metrics.calmar_ratio)} |",
        f"| Maximum Drawdown | {metrics.max_drawdown:.2%} |",
        f"| Maximum Drawdown Duration | {metrics.max_drawdown_duration} periods |",
        "",
        "## Van Tharp System Quality",
        "",
        f"- SQN\u2081\u2080\u2080: **{_format_number(sqn.sqn_100)}** ({sqn.rating})",
        f"- Valid R-multiples: **{sqn.trade_count}**",
        f"- Mean R / standard deviation: **{sqn.mean_r:.4f} / {sqn.std_r:.4f}**",
        f"- One-sided mean-R p-value: **{sqn.p_value:.6f}**; "
        f"p < 0.05: **{'yes' if sqn.significant else 'no'}**",
    ]
    if dsr_result is not None or cpcv_result is not None:
        lines.extend(["", "## Overfitting Diagnostics", ""])
    if dsr_result is not None:
        lines.extend(
            [
                "### Deflated Sharpe Ratio",
                "",
                f"- DSR probability: **{dsr_result.probability:.2%}**",
                f"- Observed / deflated benchmark Sharpe: "
                f"**{dsr_result.observed_sharpe:.4f} / {dsr_result.benchmark_sharpe:.4f}**",
                f"- Trials / observations: **{dsr_result.trial_count} / "
                f"{dsr_result.sample_length}**",
                f"- p-value: **{dsr_result.p_value:.6f}**; p < 0.05: "
                f"**{'yes' if dsr_result.significant else 'no'}**",
                "",
            ]
        )
    if cpcv_result is not None:
        lines.extend(
            [
                "### Probability of Backtest Overfitting",
                "",
                f"- PBO: **{cpcv_result.pbo:.2%}** across "
                f"**{cpcv_result.n_splits}** purged combinations",
                f"- Randomization p-value: **{cpcv_result.p_value:.6f}**; p < 0.05: "
                f"**{'yes' if cpcv_result.significant else 'no'}**",
                f"- P-value method: **{cpcv_result.p_value_method}**",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def export_equity_curve_html(
    equity_curve: pd.Series,
    output_path: str | Path,
    *,
    title: str = "Backtest Equity Curve",
) -> Path:
    """Write a self-contained Plotly equity/drawdown report."""
    from plotly import graph_objects as go
    from plotly.subplots import make_subplots

    analytics = PerformanceAnalytics(equity_curve)
    peaks = equity_curve.cummax()
    drawdown = equity_curve / peaks - 1.0
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.7, 0.3],
        subplot_titles=("Equity", "Drawdown"),
    )
    figure.add_trace(
        go.Scatter(
            x=analytics.equity_curve.index,
            y=analytics.equity_curve.values,
            name="Equity",
            mode="lines",
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=drawdown.index,
            y=drawdown.values,
            name="Drawdown",
            mode="lines",
            fill="tozeroy",
        ),
        row=2,
        col=1,
    )
    figure.update_layout(title=title, template="plotly_white", hovermode="x unified")
    figure.update_yaxes(tickformat=",.2f", row=1, col=1)
    figure.update_yaxes(tickformat=".1%", row=2, col=1)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.write_html(path, include_plotlyjs=True, full_html=True)
    return path


def _format_number(value: float) -> str:
    if np.isposinf(value):
        return "infinite"
    if np.isneginf(value):
        return "-infinite"
    return f"{value:.4f}"


__all__ = [
    "CPCVResult",
    "CombinatorialPurgedCV",
    "DSRDiagnostics",
    "DeflatedSharpeResult",
    "PerformanceAnalytics",
    "PerformanceMetrics",
    "PurgedSplit",
    "SQNResult",
    "SharpeProbabilityResult",
    "apply_embargo",
    "export_equity_curve_html",
    "generate_markdown_summary",
    "purge_overlapping_events",
]

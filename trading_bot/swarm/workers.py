"""Concrete swarm worker implementations."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from trading_bot.swarm.base import BaseSwarmWorker, WorkerConfig, WorkerResult, WorkerState
from trading_bot.swarm.results import SignalVote

logger = logging.getLogger(__name__)


class TechnicalAnalystWorker(BaseSwarmWorker):
    """Technical analysis worker using indicators and chart patterns."""

    def __init__(self, config: WorkerConfig) -> None:
        super().__init__(config)

    def execute(
        self,
        symbols: list[str],
        market_data: dict[str, Any],
        portfolio_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> WorkerResult:
        signals = []
        analysis_parts = []
        ticker_results = {}

        for ticker in symbols:
            frame = market_data.get(ticker)
            if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
                continue

            signal = self._analyze_ticker(ticker, frame)
            if signal:
                signals.append(signal.model_dump())
                ticker_results[ticker] = signal.model_dump()

            analysis_parts.append(
                f"{ticker}: {self._compute_technical_rating(frame)}"
            )

        return WorkerResult(
            worker_name=self.config.name,
            preset=self.config.preset,
            state=WorkerState.DONE,
            signals=signals,
            analysis="\n".join(analysis_parts),
            ticker_results=ticker_results,
            data={
                "workers_analyzed": len(symbols),
                "signals_generated": len(signals),
            },
        )

    def _analyze_ticker(self, ticker: str, frame: pd.DataFrame) -> SignalVote | None:
        """Generate technical signal for a single ticker."""
        try:
            latest = frame.iloc[-1]
            close = float(latest.get("close", 0))
            if close <= 0:
                return None

            # Compute key indicators
            indicators = self._compute_indicators(frame)

            # Trend analysis
            trend = "bullish" if indicators["ema_trend"] > 0 else "bearish"

            # Momentum analysis
            momentum = "overbought" if indicators["rsi"] > 70 else (
                "oversold" if indicators["rsi"] < 30 else "neutral"
            )

            # Volume analysis
            volume_ratio = indicators.get("volume_ratio", 1.0)
            volume_signal = "high_volume" if volume_ratio > 1.5 else "normal"

            # Combine signals
            score = 0
            reasons = []

            if indicators["ema_trend"] > 0:
                score += 1
                reasons.append("uptrend confirmed")
            else:
                score -= 1

            if indicators["price_momentum"] > 0:
                score += 1
                reasons.append("positive price momentum")
            elif indicators["price_momentum"] < 0 and indicators["ema_trend"] <= 0:
                score -= 1

            if indicators["rsi"] < 40:
                score += 1
                reasons.append("oversold RSI")
            elif indicators["rsi"] > 60 and indicators["ema_trend"] <= 0:
                score -= 1
                reasons.append("overbought RSI")

            if volume_ratio > 1.5:
                score += 1
                reasons.append("above average volume")

            # Determine action
            if score >= 2:
                action = "BUY"
                confidence = min(0.5 + score * 0.15, 0.95)
            elif score <= -2:
                action = "SELL"
                confidence = min(0.5 + abs(score) * 0.15, 0.95)
            else:
                action = "HOLD"
                confidence = 0.5

            return SignalVote(
                ticker=ticker,
                action=action,
                confidence=round(confidence, 2),
                worker_name=self.config.name,
                preset=self.config.preset,
                reasons=reasons,
                metadata={
                    "rsi": round(indicators["rsi"], 1),
                    "ema_trend": round(indicators["ema_trend"], 2),
                    "volume_ratio": round(volume_ratio, 2),
                    "trend": trend,
                    "momentum": momentum,
                },
            )

        except Exception as e:
            logger.warning("Technical analysis failed for %s: %s", ticker, e)
            return None

    def _compute_indicators(self, frame: pd.DataFrame) -> dict[str, float]:
        """Compute technical indicators from price data."""
        closes = frame["close"].astype(float)
        volumes = frame["volume"].astype(float) if "volume" in frame.columns else pd.Series([1.0] * len(frame))

        # RSI (14-period)
        delta = closes.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.rolling(window=14, min_periods=14).mean()
        avg_loss = loss.rolling(window=14, min_periods=14).mean()
        rs = avg_gain / avg_loss.replace(0, pd.NA)
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.where(avg_loss != 0, 100.0)
        rsi = rsi.where(avg_gain != 0, 0.0)
        rsi = rsi.where((avg_gain != 0) | (avg_loss != 0), 50.0)
        rsi_value = float(rsi.iloc[-1]) if not rsi.empty else 50.0

        # EMA trend (20-period vs 50-period)
        ema_20 = closes.ewm(span=20, adjust=False).mean()
        ema_50 = closes.ewm(span=50, adjust=False).mean()
        ema_trend = (
            float((ema_20.iloc[-1] - ema_50.iloc[-1]) / ema_50.iloc[-1] * 100)
            if len(closes) >= 50 and not ema_50.empty
            else 0.0
        )

        # Volume ratio (current vs 20-period average)
        vol_avg = volumes.rolling(window=20, min_periods=1).mean()
        volume_ratio = float(volumes.iloc[-1] / vol_avg.iloc[-1]) if not vol_avg.empty and vol_avg.iloc[-1] > 0 else 1.0
        price_momentum = float(closes.iloc[-1] / closes.iloc[-20] - 1) if len(closes) >= 20 and closes.iloc[-20] > 0 else 0.0

        return {
            "rsi": rsi_value,
            "ema_trend": ema_trend,
            "volume_ratio": volume_ratio,
            "price_momentum": price_momentum,
        }

    def _compute_technical_rating(self, frame: pd.DataFrame) -> str:
        """Compute a simple technical rating."""
        indicators = self._compute_indicators(frame)
        score = 0

        if indicators["ema_trend"] > 0:
            score += 1
        if indicators["price_momentum"] > 0:
            score += 1
        elif indicators["price_momentum"] < 0 and indicators["ema_trend"] <= 0:
            score -= 1
        if indicators["rsi"] < 40:
            score += 1
        elif indicators["rsi"] > 60 and indicators["ema_trend"] <= 0:
            score -= 1
        if indicators["volume_ratio"] > 1.5:
            score += 1

        if score >= 2:
            return "Bullish"
        elif score <= -1:
            return "Bearish"
        else:
            return "Neutral"


class RiskManagerWorker(BaseSwarmWorker):
    """Risk assessment worker evaluating portfolio impact and risk metrics."""

    def __init__(self, config: WorkerConfig) -> None:
        super().__init__(config)

    def execute(
        self,
        symbols: list[str],
        market_data: dict[str, Any],
        portfolio_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> WorkerResult:
        risks = []
        analysis_parts = []
        signals = []
        ticker_results = {}
        worker_results = kwargs.get("worker_results", {})

        # Portfolio-level risk checks
        if portfolio_state:
            portfolio_risks = self._assess_portfolio_risk(portfolio_state)
            risks.extend(portfolio_risks)
            analysis_parts.append(f"Portfolio risks: {len(portfolio_risks)} issues found")

        # Position-level risk checks
        for ticker in symbols:
            frame = market_data.get(ticker)
            if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
                continue

            ticker_risks = self._assess_ticker_risk(ticker, frame, portfolio_state)
            risks.extend(ticker_risks)
            technical_context = self._worker_vote_context(
                worker_results,
                "technical_analyst",
                ticker,
            )
            fundamental_context = self._worker_vote_context(
                worker_results,
                "fundamental_analyst",
                ticker,
            )

            # Generate risk-adjusted signal
            signal = self._risk_adjusted_signal(
                ticker,
                frame,
                ticker_risks,
                technical_context,
                fundamental_context,
            )
            if signal:
                signals.append(signal.model_dump())
                ticker_results[ticker] = signal.model_dump()

            analysis_parts.append(f"{ticker}: {len(ticker_risks)} risk factors")

        return WorkerResult(
            worker_name=self.config.name,
            preset=self.config.preset,
            state=WorkerState.DONE,
            signals=signals,
            analysis="\n".join(analysis_parts),
            ticker_results=ticker_results,
            data={
                "risks": risks,
                "total_risks": len(risks),
                "upstream_workers": sorted(worker_results),
            },
        )

    def _worker_vote_context(
        self,
        worker_results: dict[str, Any],
        worker_name: str,
        ticker: str,
    ) -> dict[str, Any]:
        result = worker_results.get(worker_name)
        if not isinstance(result, WorkerResult):
            return {}
        vote = result.ticker_results.get(ticker)
        return vote if isinstance(vote, dict) else {}

    def _assess_portfolio_risk(self, portfolio_state: dict[str, Any]) -> list[str]:
        """Assess portfolio-level risks."""
        risks = []

        cash = portfolio_state.get("cash", 0)
        equity = portfolio_state.get("equity", 1)
        positions = portfolio_state.get("positions", {})

        # Concentration risk
        if positions:
            max_position_pct = max(
                (p.get("quantity", 0) * p.get("average_cost", 0) / equity * 100
                 for p in positions.values()),
                default=0,
            )
            if max_position_pct > 20:
                risks.append(f"High concentration: largest position {max_position_pct:.1f}%")

        # Cash buffer
        cash_ratio = cash / equity if equity > 0 else 0
        if cash_ratio < 0.1:
            risks.append("Low cash buffer (<10%)")

        return risks

    def _assess_ticker_risk(
        self,
        ticker: str,
        frame: pd.DataFrame,
        portfolio_state: dict[str, Any] | None,
    ) -> list[str]:
        """Assess risks for a single ticker."""
        risks = []

        try:
            closes = frame["close"].astype(float)
            volumes = frame["volume"].astype(float) if "volume" in frame.columns else pd.Series([1.0] * len(frame))

            # Volatility risk
            returns = closes.pct_change().dropna()
            if len(returns) >= 20:
                volatility = returns.tail(20).std() * (252 ** 0.5)
                if volatility > 0.5:
                    risks.append(f"High annualized volatility: {volatility:.0%}")

            # Liquidity risk
            avg_volume = volumes.tail(20).mean()
            if avg_volume < 100000:
                risks.append(f"Low average volume: {avg_volume:.0f}")

            # Price stability
            if len(closes) >= 10:
                max_drawdown = (closes.tail(10).max() - closes.tail(10).min()) / closes.tail(10).max()
                if max_drawdown > 0.15:
                    risks.append(f"Recent drawdown: {max_drawdown:.0%}")

        except Exception as e:
            risks.append(f"Risk assessment error: {e}")

        return risks

    def _risk_adjusted_signal(
        self,
        ticker: str,
        frame: pd.DataFrame,
        risks: list[str],
        technical_context: dict[str, Any] | None = None,
        fundamental_context: dict[str, Any] | None = None,
    ) -> SignalVote | None:
        """Generate risk-adjusted signal."""
        try:
            close = float(frame.iloc[-1]["close"])
            if close <= 0:
                return None

            # Reduce confidence based on risk count
            risk_penalty = min(len(risks) * 0.1, 0.5)
            base_confidence = 0.6
            confidence = max(base_confidence - risk_penalty, 0.2)

            # High risk suggests HOLD or SELL
            if len(risks) >= 3:
                action = "HOLD"
            elif len(risks) >= 2:
                action = "HOLD"
                confidence *= 0.8
            else:
                action = "BUY"
                confidence *= 1.1
                confidence = min(confidence, 0.9)

            return SignalVote(
                ticker=ticker,
                action=action,
                confidence=round(confidence, 2),
                worker_name=self.config.name,
                preset=self.config.preset,
                reasons=risks,
                metadata={
                    "risk_count": len(risks),
                    "technical_action": (technical_context or {}).get("action"),
                    "technical_confidence": (technical_context or {}).get("confidence"),
                    "fundamental_action": (fundamental_context or {}).get("action"),
                    "fundamental_confidence": (fundamental_context or {}).get("confidence"),
                },
            )

        except Exception as e:
            logger.warning("Risk signal failed for %s: %s", ticker, e)
            return None


class QuantFactorWorker(BaseSwarmWorker):
    """Quantitative factor model worker."""

    def __init__(self, config: WorkerConfig) -> None:
        super().__init__(config)

    def execute(
        self,
        symbols: list[str],
        market_data: dict[str, Any],
        portfolio_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> WorkerResult:
        signals = []
        analysis_parts = []
        ticker_results = {}

        for ticker in symbols:
            frame = market_data.get(ticker)
            if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
                continue

            factor_scores = self._compute_factor_scores(frame)
            signal = self._factor_to_signal(ticker, factor_scores)

            if signal:
                signals.append(signal.model_dump())
                ticker_results[ticker] = signal.model_dump()

            analysis_parts.append(
                f"{ticker}: momentum={factor_scores['momentum']:.2f}, "
                f"value={factor_scores['value']:.2f}, "
                f"quality={factor_scores['quality']:.2f}"
            )

        return WorkerResult(
            worker_name=self.config.name,
            preset=self.config.preset,
            state=WorkerState.DONE,
            signals=signals,
            analysis="\n".join(analysis_parts),
            ticker_results=ticker_results,
            data={"factors_computed": ["momentum", "value", "quality"]},
        )

    def _compute_factor_scores(self, frame: pd.DataFrame) -> dict[str, float]:
        """Compute factor scores for a ticker."""
        closes = frame["close"].astype(float)
        volumes = frame["volume"].astype(float) if "volume" in frame.columns else pd.Series([1.0] * len(frame))

        # Momentum factor (12-1 month return)
        if len(closes) >= 252:
            momentum = (closes.iloc[-1] / closes.iloc[-22] - 1)
        elif len(closes) >= 63:
            momentum = (closes.iloc[-1] / closes.iloc[-21] - 1)
        else:
            momentum = 0.0

        # Mean-reversion factor (distance from moving average)
        if len(closes) >= 50:
            sma_50 = closes.rolling(50).mean().iloc[-1]
            value = (sma_50 - closes.iloc[-1]) / sma_50
        else:
            value = 0.0

        # Quality factor (volume-price correlation)
        if len(closes) >= 20:
            returns = closes.pct_change().dropna()
            vol_returns = volumes.pct_change().dropna()
            common_len = min(len(returns), len(vol_returns))
            if common_len >= 10:
                quality = returns.tail(common_len).corr(vol_returns.tail(common_len))
            else:
                quality = 0.0
        else:
            quality = 0.0

        return {
            "momentum": float(momentum),
            "value": float(value),
            "quality": float(quality),
        }

    def _factor_to_signal(
        self,
        ticker: str,
        factors: dict[str, float],
    ) -> SignalVote | None:
        """Convert factor scores to trading signal."""
        try:
            close = 100.0  # Placeholder for price
            score = 0

            if factors["momentum"] > 0.05:
                score += 1
            elif factors["momentum"] < -0.05:
                score -= 1

            if factors["value"] > 0.02:
                score += 1
            elif factors["value"] < -0.02:
                score -= 1

            if factors["quality"] > 0.3:
                score += 1
            elif factors["quality"] < -0.3:
                score -= 1

            if score >= 2:
                action = "BUY"
                confidence = min(0.5 + score * 0.15, 0.9)
            elif score <= -2:
                action = "SELL"
                confidence = min(0.5 + abs(score) * 0.15, 0.9)
            else:
                action = "HOLD"
                confidence = 0.5

            return SignalVote(
                ticker=ticker,
                action=action,
                confidence=round(confidence, 2),
                worker_name=self.config.name,
                preset=self.config.preset,
                reasons=[f"{k}={v:.3f}" for k, v in factors.items()],
                metadata=factors,
            )

        except Exception as e:
            logger.warning("Factor signal failed for %s: %s", ticker, e)
            return None


class FundamentalAnalystWorker(BaseSwarmWorker):
    """Fundamental analysis worker using price-based proxies."""

    def __init__(self, config: WorkerConfig) -> None:
        super().__init__(config)

    def execute(
        self,
        symbols: list[str],
        market_data: dict[str, Any],
        portfolio_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> WorkerResult:
        signals = []
        analysis_parts = []
        ticker_results = {}

        for ticker in symbols:
            frame = market_data.get(ticker)
            if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
                continue

            fundamentals = self._compute_fundamentals(frame)
            signal = self._fundamentals_to_signal(ticker, fundamentals)

            if signal:
                signals.append(signal.model_dump())
                ticker_results[ticker] = signal.model_dump()

            analysis_parts.append(
                f"{ticker}: quality={fundamentals['quality']:.2f}, "
                f"value={fundamentals['value']:.2f}, "
                f"growth={fundamentals['growth']:.2f}"
            )

        return WorkerResult(
            worker_name=self.config.name,
            preset=self.config.preset,
            state=WorkerState.DONE,
            signals=signals,
            analysis="\n".join(analysis_parts),
            ticker_results=ticker_results,
            data={"fundamentals_computed": ["quality", "value", "growth"]},
        )

    def _compute_fundamentals(self, frame: pd.DataFrame) -> dict[str, float]:
        """Compute fundamental proxies from price/volume data."""
        closes = frame["close"].astype(float)
        volumes = frame["volume"].astype(float) if "volume" in frame.columns else pd.Series([1.0] * len(frame))

        # Quality factor: consistency of returns (low variance = high quality)
        returns = closes.pct_change().dropna()
        if len(returns) >= 60:
            rolling_std = returns.rolling(60).std()
            quality_score = -float(rolling_std.iloc[-1] * 10)  # Negative because lower is better
            quality_score = max(-1.0, min(1.0, quality_score))
        else:
            quality_score = 0.0

        # Value factor: price relative to moving averages (mean reversion potential)
        if len(closes) >= 200:
            sma_200 = closes.rolling(200).mean().iloc[-1]
            if sma_200 > 0:
                value = (sma_200 - closes.iloc[-1]) / sma_200  # Positive = undervalued
            else:
                value = 0.0
        elif len(closes) >= 100:
            sma_100 = closes.rolling(100).mean().iloc[-1]
            if sma_100 > 0:
                value = (sma_100 - closes.iloc[-1]) / sma_100
            else:
                value = 0.0
        else:
            value = 0.0
        value = max(-1.0, min(1.0, value * 5))  # Scale to -1 to 1

        # Growth factor: multi-timeframe momentum
        if len(closes) >= 252:
            mom_1y = closes.iloc[-1] / closes.iloc[-252] - 1
            mom_3m = closes.iloc[-1] / closes.iloc[-63] - 1
            growth = (mom_1y + mom_3m) / 2
        elif len(closes) >= 63:
            mom_3m = closes.iloc[-1] / closes.iloc[-21] - 1
            mom_1m = closes.iloc[-1] / closes.iloc[-21] - 1
            growth = (mom_3m + mom_1m) / 2
        else:
            growth = 0.0
        growth = max(-1.0, min(1.0, growth * 2))  # Scale to -1 to 1

        return {
            "quality": round(quality_score, 2),
            "value": round(value, 2),
            "growth": round(growth, 2),
        }

    def _fundamentals_to_signal(
        self,
        ticker: str,
        fundamentals: dict[str, float],
    ) -> SignalVote | None:
        """Convert fundamentals to trading signal."""
        try:
            score = 0
            reasons = []

            if fundamentals["quality"] > 0.3:
                score += 1
                reasons.append("high quality (consistent returns)")
            elif fundamentals["quality"] < -0.3:
                score -= 1
                reasons.append("low quality (volatile returns)")

            if fundamentals["value"] > 0.2:
                score += 1
                reasons.append("undervalued (below MA)")
            elif fundamentals["value"] < -0.2:
                score -= 1
                reasons.append("overvalued (above MA)")

            if fundamentals["growth"] > 0.2:
                score += 1
                reasons.append("positive growth momentum")
            elif fundamentals["growth"] < -0.2:
                score -= 1
                reasons.append("negative growth momentum")

            if score >= 2:
                action = "BUY"
                confidence = min(0.5 + score * 0.15, 0.9)
            elif score <= -2:
                action = "SELL"
                confidence = min(0.5 + abs(score) * 0.15, 0.9)
            else:
                action = "HOLD"
                confidence = 0.5

            return SignalVote(
                ticker=ticker,
                action=action,
                confidence=round(confidence, 2),
                worker_name=self.config.name,
                preset=self.config.preset,
                reasons=reasons,
                metadata=fundamentals,
            )

        except Exception as e:
            logger.warning("Fundamental analysis failed for %s: %s", ticker, e)
            return None


class MacroStrategistWorker(BaseSwarmWorker):
    """Macro strategy worker analyzing market regime and sector rotation."""

    def __init__(self, config: WorkerConfig) -> None:
        super().__init__(config)

    def execute(
        self,
        symbols: list[str],
        market_data: dict[str, Any],
        portfolio_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> WorkerResult:
        signals = []
        analysis_parts = []
        ticker_results = {}

        # Detect overall market regime from first symbol (proxy for market)
        market_regime = self._detect_market_regime(market_data)
        analysis_parts.append(f"Market regime: {market_regime}")

        for ticker in symbols:
            frame = market_data.get(ticker)
            if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
                continue

            regime_signal = self._assess_regime_fit(ticker, frame, market_regime)
            if regime_signal:
                signals.append(regime_signal.model_dump())
                ticker_results[ticker] = regime_signal.model_dump()

            analysis_parts.append(f"{ticker}: regime={regime_signal.action} (market: {market_regime})")

        return WorkerResult(
            worker_name=self.config.name,
            preset=self.config.preset,
            state=WorkerState.DONE,
            signals=signals,
            analysis="\n".join(analysis_parts),
            ticker_results=ticker_results,
            data={"market_regime": market_regime},
        )

    def _detect_market_regime(self, market_data: dict[str, Any]) -> str:
        """Detect current market regime using available data."""
        for ticker, frame in market_data.items():
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            closes = frame["close"].astype(float)
            if len(closes) < 50:
                continue

            sma_50 = closes.rolling(50).mean().iloc[-1]
            current_price = closes.iloc[-1]
            price_vs_sma50 = (current_price - sma_50) / sma_50

            returns = closes.pct_change().dropna()
            if len(returns) >= 20:
                volatility = returns.tail(20).std() * (252 ** 0.5)
            else:
                volatility = 0.2

            if price_vs_sma50 > 0.05 and volatility < 0.25:
                return "bull_trend"
            elif price_vs_sma50 < -0.05 or volatility > 0.4:
                return "bear_trend"
            else:
                return "range_bound"

        return "unknown"

    def _assess_regime_fit(
        self,
        ticker: str,
        frame: pd.DataFrame,
        market_regime: str,
    ) -> SignalVote:
        """Assess whether ticker fits current market regime."""
        closes = frame["close"].astype(float)
        volume = frame["volume"].astype(float) if "volume" in frame.columns else pd.Series([1.0] * len(frame))

        if len(closes) < 50:
            return SignalVote(
                ticker=ticker,
                action="HOLD",
                confidence=0.5,
                worker_name=self.config.name,
                preset=self.config.preset,
                reasons=["insufficient data"],
                metadata={"market_regime": market_regime},
            )

        sma_50 = closes.rolling(50).mean().iloc[-1]
        sma_200 = closes.rolling(200).mean().iloc[-1] if len(closes) >= 200 else sma_50
        current_price = closes.iloc[-1]

        price_vs_sma50 = (current_price - sma_50) / sma_50 if sma_50 > 0 else 0
        golden_cross = sma_50 > sma_200 if sma_200 > 0 else False

        if market_regime == "bull_trend":
            if price_vs_sma50 > 0 and golden_cross:
                action = "BUY"
                confidence = 0.8
            elif price_vs_sma50 > 0:
                action = "BUY"
                confidence = 0.6
            else:
                action = "HOLD"
                confidence = 0.5
        elif market_regime == "bear_trend":
            if price_vs_sma50 < 0:
                action = "SELL"
                confidence = 0.7
            else:
                action = "HOLD"
                confidence = 0.6
        else:
            if price_vs_sma50 > 0.03:
                action = "SELL"
                confidence = 0.6
            elif price_vs_sma50 < -0.03:
                action = "BUY"
                confidence = 0.6
            else:
                action = "HOLD"
                confidence = 0.5

        reasons = [f"market={market_regime}"]
        if golden_cross:
            reasons.append("golden_cross")
        if price_vs_sma50 > 0:
            reasons.append("above_sma50")
        elif price_vs_sma50 < 0:
            reasons.append("below_sma50")

        return SignalVote(
            ticker=ticker,
            action=action,
            confidence=round(confidence, 2),
            worker_name=self.config.name,
            preset=self.config.preset,
            reasons=reasons,
            metadata={
                "market_regime": market_regime,
                "price_vs_sma50": round(price_vs_sma50, 4),
                "golden_cross": golden_cross,
            },
        )


class PatternRecognizerWorker(BaseSwarmWorker):
    """Chart pattern recognition worker."""

    def __init__(self, config: WorkerConfig) -> None:
        super().__init__(config)

    def execute(
        self,
        symbols: list[str],
        market_data: dict[str, Any],
        portfolio_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> WorkerResult:
        signals = []
        analysis_parts = []
        ticker_results = {}

        for ticker in symbols:
            frame = market_data.get(ticker)
            if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
                continue

            patterns = self._detect_patterns(frame)
            signal = self._patterns_to_signal(ticker, patterns)

            if signal:
                signals.append(signal.model_dump())
                ticker_results[ticker] = signal.model_dump()

            pattern_names = [p["name"] for p in patterns]
            analysis_parts.append(f"{ticker}: patterns={pattern_names}")

        return WorkerResult(
            worker_name=self.config.name,
            preset=self.config.preset,
            state=WorkerState.DONE,
            signals=signals,
            analysis="\n".join(analysis_parts),
            ticker_results=ticker_results,
            data={"patterns_detected": ["double_bottom", "head_shoulders", "triangle", "flag"]},
        )

    def _detect_patterns(self, frame: pd.DataFrame) -> list[dict[str, Any]]:
        """Detect common chart patterns."""
        patterns = []
        closes = frame["close"].astype(float)
        highs = frame["high"].astype(float)
        lows = frame["low"].astype(float)

        if len(closes) < 30:
            return patterns

        if self._is_double_bottom(closes, lows):
            patterns.append({
                "name": "double_bottom",
                "type": "bullish_reversal",
                "confidence": 0.7,
            })

        if self._is_double_top(closes, highs):
            patterns.append({
                "name": "double_top",
                "type": "bearish_reversal",
                "confidence": 0.7,
            })

        if self._is_ascending_triangle(closes, highs, lows):
            patterns.append({
                "name": "ascending_triangle",
                "type": "bullish_continuation",
                "confidence": 0.65,
            })

        if self._is_descending_triangle(closes, highs, lows):
            patterns.append({
                "name": "descending_triangle",
                "type": "bearish_continuation",
                "confidence": 0.65,
            })

        if self._is_bull_flag(closes, highs, lows):
            patterns.append({
                "name": "bull_flag",
                "type": "bullish_continuation",
                "confidence": 0.6,
            })

        return patterns

    def _is_double_bottom(self, closes: pd.Series, lows: pd.Series) -> bool:
        """Detect double bottom pattern."""
        if len(closes) < 40:
            return False

        recent_lows = lows.tail(40)
        lowest = recent_lows.min()
        second_lowest = recent_lows.nsmallest(2).iloc[-1]

        if lowest > 0 and abs(second_lowest - lowest) / lowest < 0.05:
            current = closes.iloc[-1]
            if current > lowest * 1.02:
                return True
        return False

    def _is_double_top(self, closes: pd.Series, highs: pd.Series) -> bool:
        """Detect double top pattern."""
        if len(closes) < 40:
            return False

        recent_highs = highs.tail(40)
        highest = recent_highs.max()
        second_highest = recent_highs.nlargest(2).iloc[-1]

        if highest > 0 and abs(second_highest - highest) / highest < 0.05:
            current = closes.iloc[-1]
            if current < highest * 0.98:
                return True
        return False

    def _is_ascending_triangle(self, closes: pd.Series, highs: pd.Series, lows: pd.Series) -> bool:
        """Detect ascending triangle pattern."""
        if len(closes) < 30:
            return False

        recent = 30
        resistance = highs.tail(recent).quantile(0.85)
        support_trend = lows.tail(recent).rolling(5).min().diff().mean()

        current = closes.iloc[-1]
        return resistance > 0 and current > resistance * 0.97 and support_trend > 0

    def _is_descending_triangle(self, closes: pd.Series, highs: pd.Series, lows: pd.Series) -> bool:
        """Detect descending triangle pattern."""
        if len(closes) < 30:
            return False

        recent = 30
        support = lows.tail(recent).quantile(0.15)
        resistance_trend = highs.tail(recent).rolling(5).max().diff().mean()

        current = closes.iloc[-1]
        return support > 0 and current < support * 1.03 and resistance_trend < 0

    def _is_bull_flag(self, closes: pd.Series, highs: pd.Series, lows: pd.Series) -> bool:
        """Detect bull flag pattern."""
        if len(closes) < 40:
            return False

        recent = 40
        rally_start = closes.iloc[-20]
        rally_end = closes.iloc[-1]
        consolidation_high = highs.tail(20).max()
        consolidation_low = lows.tail(20).min()

        if rally_start > 0:
            rally_pct = (rally_end - rally_start) / rally_start
            consolidation_range = (consolidation_high - consolidation_low) / rally_start
            return rally_pct > 0.05 and consolidation_range < 0.15

        return False

    def _patterns_to_signal(
        self,
        ticker: str,
        patterns: list[dict[str, Any]],
    ) -> SignalVote | None:
        """Convert detected patterns to trading signal."""
        try:
            if not patterns:
                return SignalVote(
                    ticker=ticker,
                    action="HOLD",
                    confidence=0.5,
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    reasons=["no_patterns_detected"],
                    metadata={},
                )

            bullish = sum(1 for p in patterns if "bullish" in p["type"])
            bearish = sum(1 for p in patterns if "bearish" in p["type"])

            if bullish > bearish:
                action = "BUY"
                confidence = min(0.5 + bullish * 0.15, 0.9)
            elif bearish > bullish:
                action = "SELL"
                confidence = min(0.5 + bearish * 0.15, 0.9)
            else:
                action = "HOLD"
                confidence = 0.5

            reasons = [p["name"] for p in patterns]

            return SignalVote(
                ticker=ticker,
                action=action,
                confidence=round(confidence, 2),
                worker_name=self.config.name,
                preset=self.config.preset,
                reasons=reasons,
                metadata={"patterns": patterns},
            )

        except Exception as e:
            logger.warning("Pattern recognition failed for %s: %s", ticker, e)
            return None


class OnChainAnalystWorker(BaseSwarmWorker):
    """Volume and order flow analysis worker (equity-adapted)."""

    def __init__(self, config: WorkerConfig) -> None:
        super().__init__(config)

    def execute(
        self,
        symbols: list[str],
        market_data: dict[str, Any],
        portfolio_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> WorkerResult:
        signals = []
        analysis_parts = []
        ticker_results = {}

        for ticker in symbols:
            frame = market_data.get(ticker)
            if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
                continue

            flow_analysis = self._analyze_volume_flow(frame)
            signal = self._flow_to_signal(ticker, flow_analysis)

            if signal:
                signals.append(signal.model_dump())
                ticker_results[ticker] = signal.model_dump()

            analysis_parts.append(
                f"{ticker}: accumulation={flow_analysis['accumulation_score']:.2f}, "
                f"volume_trend={flow_analysis['volume_trend']:.2f}, "
                f"smart_money={flow_analysis['smart_money']}"
            )

        return WorkerResult(
            worker_name=self.config.name,
            preset=self.config.preset,
            state=WorkerState.DONE,
            signals=signals,
            analysis="\n".join(analysis_parts),
            ticker_results=ticker_results,
            data={"flow_metrics": ["accumulation_score", "volume_trend", "smart_money"]},
        )

    def _analyze_volume_flow(self, frame: pd.DataFrame) -> dict[str, Any]:
        """Analyze volume and order flow patterns."""
        closes = frame["close"].astype(float)
        opens = frame["open"].astype(float)
        highs = frame["high"].astype(float)
        lows = frame["low"].astype(float)
        volumes = frame["volume"].astype(float)

        # Money Flow Index proxy (volume-weighted price direction)
        typical_price = (closes + highs + lows) / 3.0
        money_flow = typical_price * volumes
        mf_positive = money_flow.where(closes > closes.shift(1), 0.0)
        mf_negative = money_flow.where(closes <= closes.shift(1), 0.0)

        avg_pos = mf_positive.rolling(window=14, min_periods=14).mean()
        avg_neg = mf_negative.rolling(window=14, min_periods=14).mean()
        rs = avg_pos / avg_neg.replace(0, float("inf"))
        mfi = 100 - (100 / (1 + rs))
        mfi_value = float(mfi.iloc[-1]) if not mfi.empty else 50.0

        # Accumulation/Distribution line
        clv = ((closes - lows) - (highs - closes)) / (highs - lows).replace(0, float("inf"))
        clv = clv.fillna(0.0)
        ad_line = (clv * volumes).cumsum()
        ad_slope = ad_line.pct_change(5).iloc[-1] if len(ad_line) >= 5 else 0.0

        # Volume trend (comparing recent vs historical average)
        vol_short = volumes.rolling(window=5, min_periods=1).mean()
        vol_long = volumes.rolling(window=20, min_periods=1).mean()
        volume_ratio = vol_short / vol_long.replace(0, float("inf"))
        volume_trend = float(volume_ratio.iloc[-1]) if not volume_ratio.empty else 1.0

        # Smart money indicator (large moves on high volume)
        returns = closes.pct_change()
        large_move = returns.abs() > returns.rolling(20).std().replace(0, float("inf"))
        high_volume = volumes > volumes.rolling(20).mean().replace(0, float("inf"))
        smart_money_signal = (large_move & high_volume).rolling(5).sum()
        smart_money_score = float(smart_money_signal.iloc[-1]) if not smart_money_signal.empty else 0.0

        # Accumulation score (-1 to 1)
        accumulation_score = 0.0
        if mfi_value < 30:
            accumulation_score += 0.3  # Oversold, potential accumulation
        elif mfi_value > 70:
            accumulation_score -= 0.3  # Overbought, potential distribution
        if ad_slope > 0.01:
            accumulation_score += 0.3
        elif ad_slope < -0.01:
            accumulation_score -= 0.3
        if volume_trend > 1.5 and closes.iloc[-1] > closes.iloc[-2]:
            accumulation_score += 0.2  # Rising volume on up day
        elif volume_trend > 1.5 and closes.iloc[-1] < closes.iloc[-2]:
            accumulation_score -= 0.2  # Rising volume on down day
        accumulation_score = max(-1.0, min(1.0, accumulation_score))

        return {
            "mfi": mfi_value,
            "ad_slope": float(ad_slope),
            "volume_trend": volume_trend,
            "smart_money": smart_money_score,
            "accumulation_score": round(accumulation_score, 2),
        }

    def _flow_to_signal(
        self,
        ticker: str,
        flow: dict[str, Any],
    ) -> SignalVote | None:
        """Convert flow analysis to trading signal."""
        try:
            score = 0
            reasons = []

            if flow["accumulation_score"] > 0.5:
                score += 1
                reasons.append("accumulation detected")
            elif flow["accumulation_score"] < -0.5:
                score -= 1
                reasons.append("distribution detected")

            if flow["mfi"] < 30:
                score += 1
                reasons.append("oversold MFI")
            elif flow["mfi"] > 70:
                score -= 1
                reasons.append("overbought MFI")

            if flow["volume_trend"] > 1.5:
                score += 1
                reasons.append("above average volume")

            if flow["smart_money"] >= 2:
                score += 1
                reasons.append("smart money activity")

            if score >= 2:
                action = "BUY"
                confidence = min(0.5 + score * 0.15, 0.9)
            elif score <= -2:
                action = "SELL"
                confidence = min(0.5 + abs(score) * 0.15, 0.9)
            else:
                action = "HOLD"
                confidence = 0.5

            return SignalVote(
                ticker=ticker,
                action=action,
                confidence=round(confidence, 2),
                worker_name=self.config.name,
                preset=self.config.preset,
                reasons=reasons,
                metadata=flow,
            )

        except Exception as e:
            logger.warning("Flow analysis failed for %s: %s", ticker, e)
            return None


class TrendFollowerWorker(BaseSwarmWorker):
    """Trend-following worker using ADX, EMA alignment, and momentum."""

    def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
        signals = []
        ticker_results = {}
        for ticker in symbols:
            frame = market_data.get(ticker)
            if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            vote = self._analyze(ticker, frame)
            if vote:
                signals.append(vote.model_dump())
                ticker_results[ticker] = vote.model_dump()
        return WorkerResult(
            worker_name=self.config.name, preset=self.config.preset,
            state=WorkerState.DONE, signals=signals,
            analysis=f"Trend analysis: {len(signals)} signals",
            ticker_results=ticker_results,
        )

    def _analyze(self, ticker, frame):
        try:
            latest = frame.iloc[-1]
            close = float(latest.get("close", 0))
            if close <= 0:
                return None
            rsi = float(latest.get("rsi_14", 0) or 0)
            ema20 = float(latest.get("ema_20", 0) or 0)
            sma50 = float(latest.get("sma_50", 0) or 0)
            vr = float(latest.get("volume_avg_5", 0) or 0)
            vol = float(latest.get("volume", 0) or 0)
            score = 0
            if close > ema20 > sma50: score += 2
            if close > ema20: score += 1
            if 40 < rsi < 70: score += 1
            if vr > 0 and vol > vr: score += 1
            if score >= 3:
                return SignalVote(ticker=ticker, action="BUY", confidence=min(0.9, score / 5),
                                  worker_name=self.config.name, preset=self.config.preset,
                                  reasons=[f"trend score={score}"],
                                  metadata={"ema_trend": close > ema20, "rsi": rsi})
            if score <= 0:
                return SignalVote(ticker=ticker, action="SELL", confidence=0.4,
                                  worker_name=self.config.name, preset=self.config.preset,
                                  reasons=[f"trend score={score}"], metadata={})
            return SignalVote(ticker=ticker, action="HOLD", confidence=0.5,
                              worker_name=self.config.name, preset=self.config.preset,
                              reasons=[f"trend score={score}"], metadata={})
        except Exception:
            return None


class MeanReversionWorker(BaseSwarmWorker):
    """Detects oversold reversion opportunities using %B, RSI, and VWAP."""

    def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
        signals = []
        ticker_results = {}
        for ticker in symbols:
            frame = market_data.get(ticker)
            if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            vote = self._analyze(ticker, frame)
            if vote:
                signals.append(vote.model_dump())
                ticker_results[ticker] = vote.model_dump()
        return WorkerResult(
            worker_name=self.config.name, preset=self.config.preset,
            state=WorkerState.DONE, signals=signals,
            analysis=f"Mean reversion: {len(signals)} signals",
            ticker_results=ticker_results,
        )

    def _analyze(self, ticker, frame):
        try:
            latest = frame.iloc[-1]
            close = float(latest.get("close", 0))
            if close <= 0:
                return None
            rsi = float(latest.get("rsi_14", 0) or 0)
            bb_l = float(latest.get("bb_lower", 0) or 0)
            bb_u = float(latest.get("bb_upper", 0) or 0)
            score = 0
            if bb_u > bb_l:
                pct_b = (close - bb_l) / (bb_u - bb_l) * 100
                if pct_b < 0: score += 3
                elif pct_b < 10: score += 2
                elif pct_b < 20: score += 1
            if rsi < 30: score += 2
            elif rsi < 35: score += 1
            if score >= 3:
                return SignalVote(ticker=ticker, action="BUY", confidence=min(0.9, score / 5),
                                  worker_name=self.config.name, preset=self.config.preset,
                                  reasons=[f"mr score={score} rsi={rsi}"],
                                  metadata={"rsi": rsi, "pct_b": round((close - bb_l) / (bb_u - bb_l) * 100, 1) if bb_u > bb_l else 0})
            return SignalVote(ticker=ticker, action="HOLD", confidence=0.5,
                              worker_name=self.config.name, preset=self.config.preset,
                              reasons=[f"mr score={score}"], metadata={})
        except Exception:
            return None


class VolumeAnalystWorker(BaseSwarmWorker):
    """Evaluates volume profile, volume trend, and accumulation/distribution."""

    def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
        signals = []
        ticker_results = {}
        for ticker in symbols:
            frame = market_data.get(ticker)
            if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            vote = self._analyze(ticker, frame)
            if vote:
                signals.append(vote.model_dump())
                ticker_results[ticker] = vote.model_dump()
        return WorkerResult(
            worker_name=self.config.name, preset=self.config.preset,
            state=WorkerState.DONE, signals=signals,
            analysis=f"Volume analysis: {len(signals)} signals",
            ticker_results=ticker_results,
        )

    def _analyze(self, ticker, frame):
        try:
            latest = frame.iloc[-1]
            vol = float(latest.get("volume", 0) or 0)
            avg_vol = float(latest.get("volume_avg_5", 0) or 0)
            close = float(latest.get("close", 0) or 0)
            open_p = float(latest.get("open", 0) or 0)
            if avg_vol <= 0:
                return None
            vr = vol / avg_vol
            score = 0
            if vr > 2.0: score += 3
            elif vr > 1.5: score += 2
            elif vr > 1.0: score += 1
            if close > open_p: score += 1
            if score >= 3:
                return SignalVote(ticker=ticker, action="BUY", confidence=min(0.9, score / 4),
                                  worker_name=self.config.name, preset=self.config.preset,
                                  reasons=[f"vol score={score} vr={vr:.1f}"],
                                  metadata={"volume_ratio": round(vr, 2)})
            if vr < 0.5:
                return SignalVote(ticker=ticker, action="SELL", confidence=0.5,
                                  worker_name=self.config.name, preset=self.config.preset,
                                  reasons=[f"low volume vr={vr:.1f}"], metadata={})
            return SignalVote(ticker=ticker, action="HOLD", confidence=0.5,
                              worker_name=self.config.name, preset=self.config.preset,
                              reasons=[f"vol score={score}"], metadata={})
        except Exception:
            return None


class TechnicalConsensusWorker(BaseSwarmWorker):
    """Reads upstream trend_follower, mean_reversion, volume_analyst, and pattern_recognizer
    votes. Returns majority consensus or HOLD when disagreement is high."""

    def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
        signals = []
        ticker_results = {}
        worker_results = kwargs.get("worker_results", {})
        for ticker in symbols:
            buys = sells = holds = 0
            for wname in ("trend_follower", "mean_reversion", "volume_analyst", "pattern_recognizer"):
                wr = worker_results.get(wname)
                if wr is None:
                    continue
                ticker_vote = wr.ticker_results.get(ticker) if isinstance(wr.ticker_results, dict) else None
                if ticker_vote is None:
                    continue
                action = ticker_vote.get("action", "") if isinstance(ticker_vote, dict) else ""
                if action == "BUY": buys += 1
                elif action == "SELL": sells += 1
                else: holds += 1
            total = buys + sells + holds
            if total == 0:
                continue
            if buys > sells and buys >= 2:
                conf = buys / total
                vote = SignalVote(ticker=ticker, action="BUY", confidence=conf,
                                  worker_name=self.config.name, preset=self.config.preset,
                                  reasons=[f"consensus BUY {buys}/{total}"],
                                  metadata={"buys": buys, "sells": sells, "holds": holds})
            elif sells > buys and sells >= 2:
                conf = sells / total
                vote = SignalVote(ticker=ticker, action="SELL", confidence=conf,
                                  worker_name=self.config.name, preset=self.config.preset,
                                  reasons=[f"consensus SELL {sells}/{total}"],
                                  metadata={"buys": buys, "sells": sells, "holds": holds})
            else:
                vote = SignalVote(ticker=ticker, action="HOLD", confidence=0.5,
                                  worker_name=self.config.name, preset=self.config.preset,
                                  reasons=[f"consensus HOLD {buys}B/{sells}S/{holds}H"],
                                  metadata={"buys": buys, "sells": sells, "holds": holds})
            signals.append(vote.model_dump())
            ticker_results[ticker] = vote.model_dump()
        return WorkerResult(
            worker_name=self.config.name, preset=self.config.preset,
            state=WorkerState.DONE, signals=signals,
            analysis=f"Technical consensus: {len(signals)} tickers",
            ticker_results=ticker_results,
        )


# Worker class registry
WORKER_CLASSES: dict[str, type[BaseSwarmWorker]] = {
    "technical_analyst": TechnicalAnalystWorker,
    "risk_manager": RiskManagerWorker,
    "factor_model": QuantFactorWorker,
    "fundamental_analyst": FundamentalAnalystWorker,
    "macro_strategist": MacroStrategistWorker,
    "pattern_recognizer": PatternRecognizerWorker,
    "on_chain_analyst": OnChainAnalystWorker,
    "trend_follower": TrendFollowerWorker,
    "mean_reversion": MeanReversionWorker,
    "volume_analyst": VolumeAnalystWorker,
    "technical_consensus": TechnicalConsensusWorker,
}


def get_worker_class(worker_name: str) -> type[BaseSwarmWorker]:
    """Get worker class by name."""
    if worker_name not in WORKER_CLASSES:
        available = ", ".join(WORKER_CLASSES.keys())
        raise ValueError(
            f"Unknown worker '{worker_name}'. Available: {available}"
        )
    return WORKER_CLASSES[worker_name]

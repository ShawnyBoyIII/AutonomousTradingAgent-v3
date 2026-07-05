"""Swarm preset definitions for different analysis teams."""

from __future__ import annotations

from trading_bot.swarm.base import WorkerConfig

# Investment Committee - comprehensive multi-factor analysis
INVESTMENT_COMMITTEE = [
    WorkerConfig(
        name="technical_analyst",
        preset="investment_committee",
        description="Analyzes price action, trends, momentum, and chart patterns",
        priority=1,
        accuracy_weight=1.1,
    ),
    WorkerConfig(
        name="fundamental_analyst",
        preset="investment_committee",
        description="Evaluates financial health, valuation metrics, and growth prospects",
        priority=1,
        accuracy_weight=1.0,
    ),
    WorkerConfig(
        name="sentiment_analyst",
        preset="investment_committee",
        description="Scores news/sentiment context and market-wide fear indicators",
        priority=1,
        accuracy_weight=0.9,
    ),
    WorkerConfig(
        name="risk_manager",
        preset="investment_committee",
        description="Assesses portfolio impact, position sizing, and risk metrics",
        priority=2,
        depends_on=["technical_analyst", "fundamental_analyst", "sentiment_analyst"],
        accuracy_weight=1.15,
    ),
    WorkerConfig(
        name="macro_strategist",
        preset="investment_committee",
        description="Evaluates macroeconomic conditions and sector rotation",
        priority=0,
        accuracy_weight=0.95,
    ),
]

# Quant Desk - quantitative signal generation
QUANT_DESK = [
    WorkerConfig(
        name="factor_model",
        preset="quant_desk",
        description="Multi-factor model evaluation (value, momentum, quality, volatility)",
        priority=1,
    ),
    WorkerConfig(
        name="statistical_arb",
        preset="quant_desk",
        description="Statistical arbitrage and mean reversion signals",
        priority=1,
    ),
    WorkerConfig(
        name="ml_predictor",
        preset="quant_desk",
        description="ML model predictions (RL, gradient boosting, neural networks)",
        priority=1,
    ),
    WorkerConfig(
        name="quant_risk",
        preset="quant_desk",
        description="Quantitative risk assessment and portfolio optimization",
        priority=2,
        depends_on=["factor_model", "statistical_arb", "ml_predictor"],
    ),
]

# Risk Committee - risk-focused analysis
RISK_COMMITTEE = [
    WorkerConfig(
        name="var_analyst",
        preset="risk_committee",
        description="Value-at-Risk and stress testing analysis",
        priority=1,
    ),
    WorkerConfig(
        name="correlation_analyst",
        preset="risk_committee",
        description="Portfolio correlation and concentration risk assessment",
        priority=1,
    ),
    WorkerConfig(
        name="liquidity_analyst",
        preset="risk_committee",
        description="Liquidity risk and market impact analysis",
        priority=1,
    ),
    WorkerConfig(
        name="risk_committee_lead",
        preset="risk_committee",
        description="Risk committee final verdict and position sizing approval",
        priority=3,
        depends_on=["var_analyst", "correlation_analyst", "liquidity_analyst"],
    ),
]

# Technical Analysis Panel - deep technical analysis
TECHNICAL_ANALYSIS_PANEL = [
    WorkerConfig(
        name="trend_follower",
        preset="technical_analysis_panel",
        description="Trend identification and momentum analysis",
        priority=1,
    ),
    WorkerConfig(
        name="mean_reversion",
        preset="technical_analysis_panel",
        description="Mean reversion and contrarian signals",
        priority=1,
    ),
    WorkerConfig(
        name="volume_analyst",
        preset="technical_analysis_panel",
        description="Volume profile and order flow analysis",
        priority=1,
    ),
    WorkerConfig(
        name="pattern_recognizer",
        preset="technical_analysis_panel",
        description="Chart pattern recognition and candlestick analysis",
        priority=1,
    ),
    WorkerConfig(
        name="technical_consensus",
        preset="technical_analysis_panel",
        description="Consensus technical rating from all technical workers",
        priority=4,
        depends_on=["trend_follower", "mean_reversion", "volume_analyst", "pattern_recognizer"],
    ),
]

# Fundamental Analysis Team - fundamental evaluation
FUNDAMENTAL_ANALYSIS_TEAM = [
    WorkerConfig(
        name="valuation_expert",
        preset="fundamental_analysis_team",
        description="DCF, multiples, and intrinsic valuation",
        priority=1,
    ),
    WorkerConfig(
        name="growth_analyst",
        preset="fundamental_analysis_team",
        description="Revenue/earnings growth and margin analysis",
        priority=1,
    ),
    WorkerConfig(
        name="balance_sheet_analyst",
        preset="fundamental_analysis_team",
        description="Financial health, leverage, and cash flow analysis",
        priority=1,
    ),
    WorkerConfig(
        name="fundamental_consensus",
        preset="fundamental_analysis_team",
        description="Consensus fundamental rating",
        priority=4,
        depends_on=["valuation_expert", "growth_analyst", "balance_sheet_analyst"],
    ),
]

# Crypto Desk - cryptocurrency-specific analysis
CRYPTO_DESK = [
    WorkerConfig(
        name="on_chain_analyst",
        preset="crypto_desk",
        description="On-chain metrics and network activity analysis",
        priority=1,
    ),
    WorkerConfig(
        name="crypto_technical",
        preset="crypto_desk",
        description="Technical analysis adapted for crypto markets",
        priority=1,
    ),
    WorkerConfig(
        name="deFi_analyst",
        preset="crypto_desk",
        description="DeFi protocol metrics and tokenomics analysis",
        priority=1,
    ),
    WorkerConfig(
        name="crypto_risk",
        preset="crypto_desk",
        description="Crypto-specific risk assessment (volatility, liquidity, regulatory)",
        priority=2,
        depends_on=["on_chain_analyst", "crypto_technical", "deFi_analyst"],
    ),
]

# Macro Economics Team - macroeconomic analysis
MACRO_ECONOMICS_TEAM = [
    WorkerConfig(
        name="economic_indicator",
        preset="macro_economics_team",
        description="Economic indicators and cycle analysis",
        priority=1,
    ),
    WorkerConfig(
        name="monetary_policy",
        preset="macro_economics_team",
        description="Central bank policy and interest rate analysis",
        priority=1,
    ),
    WorkerConfig(
        name="sector_rotation",
        preset="macro_economics_team",
        description="Sector rotation and relative strength analysis",
        priority=1,
    ),
    WorkerConfig(
        name="macro_outlook",
        preset="macro_economics_team",
        description="Macro outlook and strategic positioning",
        priority=2,
        depends_on=["economic_indicator", "monetary_policy", "sector_rotation"],
    ),
]

# All available presets
ALL_PRESETS: dict[str, list[WorkerConfig]] = {
    "investment_committee": INVESTMENT_COMMITTEE,
    "quant_desk": QUANT_DESK,
    "risk_committee": RISK_COMMITTEE,
    "technical_analysis_panel": TECHNICAL_ANALYSIS_PANEL,
    "fundamental_analysis_team": FUNDAMENTAL_ANALYSIS_TEAM,
    "crypto_desk": CRYPTO_DESK,
    "macro_economics_team": MACRO_ECONOMICS_TEAM,
}


def get_preset(preset_name: str) -> list[WorkerConfig]:
    """Get worker configs for a preset by name."""
    if preset_name not in ALL_PRESETS:
        available = ", ".join(ALL_PRESETS.keys())
        raise ValueError(
            f"Unknown preset '{preset_name}'. Available: {available}"
        )
    return ALL_PRESETS[preset_name]

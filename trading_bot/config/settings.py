from pydantic import BaseModel, Field


class AppSettings(BaseModel):
    live_trading_enabled: bool = False
    timezone: str = "America/New_York"
    state_db_path: str = "state/trading_bot.db"
    universe_path: str = "state/universe.txt"
    watchlist_path: str = "state/watchlist.txt"
    universe_candidates_path: str = "state/universe_candidates.json"
    log_dir: str = "logs"
    log_level: str = "INFO"
    log_file: str | None = None
    dashboard_summary_path: str = "state/dashboard_summary.json"
    scan_results_path: str = "state/scan_results.json"
    portfolio_summary_path: str = "state/portfolio_summary.json"
    backtest_summary_path: str = "state/backtest_summary.json"
    benchmark_symbol: str | None = None


class MarketDataSettings(BaseModel):
    provider: str = "yfinance"
    providers: list[str] = Field(default_factory=list)

    @property
    def provider_stack(self) -> list[str]:
        """Ordered list of provider names to try.  Falls back to *provider*
        when *providers* is empty for backward compatibility."""
        return self.providers if self.providers else [self.provider]

    daily_period: str = "1y"
    intraday_period: str = "5d"
    intraday_interval: str = "5m"
    max_data_age_hours: int = Field(default=72, ge=1)
    max_data_age_minutes: int = Field(default=30, ge=1)  # For intraday bars
    # V2.5: Data validation settings
    validate_data: bool = Field(default=True)
    max_price_jump_pct: float = Field(default=1000.0, ge=100.0, le=5000.0)
    max_volume_jump_pct: float = Field(default=1000.0, ge=100.0, le=5000.0)
    min_bars_for_signal: int = Field(default=5, ge=1)


class ScoutSettings(BaseModel):
    screeners: list[str] = Field(
        default_factory=lambda: ["aggressive_small_caps", "small_cap_gainers"]
    )
    min_market_cap: float = Field(default=50_000_000.0, ge=0.0)
    max_market_cap: float = Field(default=2_000_000_000.0, gt=0.0)
    min_price: float = Field(default=2.0, gt=0.0)
    min_avg_dollar_volume: float = Field(default=1_000_000.0, ge=0.0)
    max_universe_size: int = Field(default=50, ge=1)
    max_snapshot_candidates: int = Field(default=100, ge=1)


class RiskSettings(BaseModel):
    max_risk_per_trade_pct: float = Field(default=0.01, gt=0.0, le=1.0)
    max_daily_risk_pct: float = Field(default=0.03, gt=0.0, le=1.0)
    max_daily_orders: int = Field(default=3, ge=1)
    max_ticker_allocation_pct: float = Field(default=0.20, gt=0.0, le=1.0)
    min_reward_risk_ratio: float = Field(default=2.0, gt=0.0)
    # V2.5: ATR-based sizing
    use_atr_sizing: bool = Field(default=True)
    atr_period: int = Field(default=14, ge=5, le=50)
    atr_multiplier: float = Field(default=2.0, ge=0.5, le=5.0)
    # Minimum stop distance = ATR × atr_stop_multiplier.
    # Prevents 0.2–0.6% noise stops on tight consolidations
    # (the 5-bar low min() stops too close to entry on breakouts).
    atr_stop_multiplier: float = Field(default=1.5, ge=0.5, le=5.0)
    # V2.5: Portfolio heat limits
    max_portfolio_heat_pct: float = Field(default=0.03, gt=0.0, le=0.5)
    # V2.5: Sector concentration
    max_sector_concentration_pct: float = Field(default=0.20, gt=0.0, le=1.0)
    # V3.1: Circuit breaker — halt trading on consecutive losses
    max_consecutive_losses: int = Field(default=5, ge=0)
    # V3.1: Circuit breaker — halt on max drawdown (% from peak)
    enable_drawdown_circuit_breaker: bool = True


class SessionSettings(BaseModel):
    close_hour: int = Field(default=16, ge=0, le=23)
    close_minute: int = Field(default=0, ge=0, le=59)
    eod_minutes_before_close: int = Field(default=5, ge=0, le=120)
    eod_enabled: bool = True


class PaperSettings(BaseModel):
    fee_per_order: float = Field(default=1.0, ge=0.0)
    slippage_bps: int = Field(default=0, ge=0)


class AlertsSettings(BaseModel):
    discord_webhook_url: str = ""
    discord_username: str = "Autonomous Trading Agent"
    slack_webhook_url: str = ""
    webhook_url: str = ""


class RobinhoodSettings(BaseModel):
    """V3: Robinhood broker configuration (MCP snapshot-based).

    Pure MCP-only mode: no direct credentials, no live auth flow, no order
    execution. The boundary consumes operator-synced snapshots and emits
    intent records for later review/execution by Codex tooling. All
    Robinhood credentials must live in the operator-managed MCP server.
    """
    enabled: bool = Field(default=False)
    # Mode settings (always shadow/read-only locally; live submit not supported)
    mode: str = Field(default="shadow")
    # Safety limits
    max_position_value: float = Field(default=10000.0, gt=0)  # Max $ per position
    daily_loss_limit: float = Field(default=100.0, gt=0)  # Halt after $ loss
    # Connection
    timeout_seconds: int = Field(default=30, ge=5, le=300)
    max_retries: int = Field(default=3, ge=0, le=10)


class StrategySettings(BaseModel):
    """V3: Dynamic strategy selection controls.

    When ``use_v3_signals`` is True, the orchestrator delegates signal
    generation to :class:`StrategySelector` (regime detection + 5-factor
    confluence scoring) instead of the legacy :mod:`intraday_signal_engine`.
    Defaults to False so the V2.5 path remains the production default until
    the V3 layer is validated in paper mode.
    """
    use_v3_signals: bool = Field(default=False)
    risk_tolerance: str = Field(default="medium")  # low, medium, high
    min_confidence: str = Field(default="medium")  # none, low, medium, high, very_high


class MonitoringSettings(BaseModel):
    """V3: Risk monitoring and alerting configuration.

    Controls for VaR, stress testing, drawdown limits, and portfolio
    correlation analysis.
    """
    var_confidence: float = Field(default=0.95, gt=0.0, le=1.0)
    var_lookback_days: int = Field(default=252, ge=20, le=500)
    max_drawdown_pct: float = Field(default=10.0, gt=0.0, le=100.0)
    max_avg_correlation: float = Field(default=0.6, gt=0.0, le=1.0)
    equity_snapshot_interval: int = Field(default=60, ge=1)  # minutes


class CounterThesisSettings(BaseModel):
    """V3: Counter-thesis analysis configuration.

    The counter-thesis engine seeks *evidence against* a BUY signal's
    thesis (divergences, exhaustion, regime mismatch, weak conviction).
    When ``enabled``, the orchestrator runs the checks before handing the
    signal to the risk manager, which vetoes blocked trades and scales
    position size down by the resulting ``confidence_multiplier``.

    Defaults to False (like ``use_v3_signals``) so the V2.5 path stays the
    production default until the layer is validated in paper mode. A missing
    data context never blocks: a data outage must not become a silent kill
    switch, so failed context fetches yield an empty result.
    """

    enabled: bool = Field(default=False)
    # A finding at or above this severity blocks the trade outright.
    block_on_severity: str = Field(default="high")  # none|low|medium|high|severe
    # If the summed confidence penalty crosses this threshold, block the trade.
    aggregate_block_threshold: float = Field(default=0.6, gt=0.0, le=1.0)
    # Manage-positions: exit open positions when the thesis is broken.
    exit_on_block: bool = Field(default=True)

    # Individual check toggles + thresholds.
    check_overbought: bool = Field(default=True)
    overbought_rsi_threshold: float = Field(default=75.0, ge=50.0, le=100.0)
    check_volume_non_confirmation: bool = Field(default=True)
    volume_confirmation_floor: float = Field(default=0.8, gt=0.0)
    check_rsi_divergence: bool = Field(default=True)
    check_resistance_proximity: bool = Field(default=True)
    resistance_bb_percent_b: float = Field(default=100.0, ge=50.0, le=200.0)
    check_regime_misalignment: bool = Field(default=True)
    check_waning_momentum: bool = Field(default=True)
    check_volatility_spike: bool = Field(default=True)
    volatility_percentile_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    check_extension: bool = Field(default=True)
    extension_pct: float = Field(default=5.0, gt=0.0)  # price-vs-EMA20 percent


class RLSettings(BaseModel):
    """RL-based trading configuration.

    When ``enabled``, the orchestrator uses a trained DRL agent to generate
    trading signals instead of rule-based engines. The agent receives normalized
    feature vectors from FeatureEngineer and outputs discrete actions
    (HOLD=0, BUY=1, SELL=2).

    Agent types: "PPO", "A2C", "SAC", "TD3", "DDPG"
    Feature sets: "standard" (19 features), "extended" (24 features)
    Reward functions: "risk_adjusted", "simple_profit", "compound_daily", "sharpe", "drawdown_penalty"
    """

    enabled: bool = Field(default=False)
    agent_type: str = Field(default="PPO")  # PPO, A2C, SAC, TD3, DDPG
    feature_set: str = Field(default="standard")  # standard, extended
    reward_function: str = Field(default="risk_adjusted")
    model_path: str = Field(default="trained_models/rl_agent.zip")
    training_episodes: int = Field(default=100, ge=1)
    training_timesteps: int = Field(default=100000, ge=1000)
    learning_rate: float = Field(default=3e-4, gt=0.0)
    max_position_pct: float = Field(default=0.20, gt=0.0, le=1.0)
    backtest_starting_cash: float = Field(default=10000.0, gt=0.0)
    backtest_max_shares: int = Field(default=100, ge=1)
    backtest_stop_loss_pct: float = Field(default=0.05, gt=0.0, lt=1.0)
    backtest_profit_target_pct: float = Field(default=0.08, gt=0.0, lt=1.0)
    action_confidence_threshold: float = Field(default=0.5, gt=0.0, le=1.0)


class SwarmSettings(BaseModel):
    """Multi-agent swarm analysis configuration.

    When ``enabled``, the orchestrator runs a swarm analysis on the
    universe before generating signals. The swarm acts as a read-only
    overlay in Phase 1: its committee decisions are logged alongside
    scanner results but do not affect trading behavior.

    Phase 2 (future): swarm confidence modifier
    Phase 3 (future): swarm veto power
    """
    enabled: bool = Field(default=False)
    preset: str = Field(default="investment_committee")
    max_workers: int = Field(default=4, ge=1, le=16)


class Settings(BaseModel):
    app: AppSettings = Field(default_factory=AppSettings)
    market_data: MarketDataSettings = Field(default_factory=MarketDataSettings)
    scout: ScoutSettings = Field(default_factory=ScoutSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    paper: PaperSettings = Field(default_factory=PaperSettings)
    alerts: AlertsSettings = Field(default_factory=AlertsSettings)
    robinhood: RobinhoodSettings = Field(default_factory=RobinhoodSettings)
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    counter_thesis: CounterThesisSettings = Field(default_factory=CounterThesisSettings)
    rl: RLSettings = Field(default_factory=RLSettings)
    swarm: SwarmSettings = Field(default_factory=SwarmSettings)

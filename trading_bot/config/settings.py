from pydantic import BaseModel, Field, field_validator


SUPPORTED_MARKET_DATA_PROVIDERS = frozenset(
    {"alpaca", "finnhub", "polygon", "yfinance"}
)


def _normalize_provider_name(value: str) -> str:
    name = str(value).strip().lower()
    if name not in SUPPORTED_MARKET_DATA_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_MARKET_DATA_PROVIDERS))
        raise ValueError(
            f"Unsupported market data provider '{value}'. "
            f"Supported providers: {supported}"
        )
    return name


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
    tuning_overrides_path: str = "state/tuning_overrides.yaml"
    advisory_dir: str = "state/advisory_learner"
    benchmark_symbol: str | None = None
    allow_yellow_mean_reversion: bool = False
    min_entry_confluence_score: float = Field(default=4.0, ge=0.0, le=12.0)
    signal_mode: str = Field(default="serial")  # "serial" or "parallel"
    min_timeframe_alignment: int = Field(default=1, ge=1, le=3)


class MarketDataSettings(BaseModel):
    provider: str = "yfinance"
    providers: list[str] = Field(default_factory=list)

    @field_validator("provider", mode="before")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        return _normalize_provider_name(value)

    @field_validator("providers", mode="before")
    @classmethod
    def _validate_providers(cls, value: list[str] | None) -> list[str]:
        if value is None:
            return []
        return [_normalize_provider_name(name) for name in value]

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
    # Wall-clock cap on a single scan iteration.  2026-07-09 incident:
    # scan hung for 7+ hours at the Polygon call chain with no global
    # deadline, blocking the burn-in's main loop and skipping EOD exits.
    scan_deadline_minutes: int = Field(default=5, ge=1, le=60)
    # V2.5: Data validation settings
    validate_data: bool = Field(default=True)
    max_price_jump_pct: float = Field(default=1000.0, ge=100.0, le=5000.0)
    max_volume_jump_pct: float = Field(default=1000.0, ge=100.0, le=5000.0)
    min_bars_for_signal: int = Field(default=5, ge=1)


class ScoutSettings(BaseModel):
    screeners: list[str] = Field(
        default_factory=lambda: ["aggressive_small_caps", "small_cap_gainers"]
    )
    min_market_cap: float = Field(default=2_000_000_000.0, ge=0.0)
    max_market_cap: float = Field(default=50_000_000_000.0, gt=0.0)
    min_price: float = Field(default=5.0, gt=0.0)
    min_avg_dollar_volume: float = Field(default=5_000_000.0, ge=0.0)
    max_universe_size: int = Field(default=50, ge=1)
    max_snapshot_candidates: int = Field(default=100, ge=1)


class AdvisorySettings(BaseModel):
    enabled: bool = Field(default=False)
    min_observations_per_symbol: int = Field(default=5, ge=1)
    main_limit: int = Field(default=10, ge=1)
    cheap_limit: int = Field(default=10, ge=1)
    cheap_stock_max_price: float = Field(default=5.0, gt=0.0)
    min_hit_rate_for_promote: float = Field(default=0.55, ge=0.0, le=1.0)


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
    atr_stop_multiplier: float = Field(default=3.0, ge=0.5, le=10.0)
    # Absolute minimum stop distance as % of entry price.
    # Overrides both the 5-bar low and ATR floor when either
    # produces a stop closer than this threshold. AGENTS.md mandates
    # a 5-minute-bar stop distance; default 3.0 is the universal floor
    # (matches config.yaml; burn-in configs override to 5.0 for stricter).
    min_stop_distance_pct: float = Field(default=3.0, ge=0.0, le=20.0)
    # V2.5: Portfolio heat limits
    max_portfolio_heat_pct: float = Field(default=0.03, gt=0.0, le=0.5)
    # V2.5: Sector concentration
    max_sector_concentration_pct: float = Field(default=0.20, gt=0.0, le=1.0)
    # 2026-07-10: Per-ticker share-count cap. The previous
    # `max_open_positions` field was rolled back per user feedback:
    # "we should be able to trade 100+ stocks; I want to limit the
    # total counts of trades per stock. Each stock can hold a maximum
    # of 50."  This cap is the absolute share limit applied AFTER ATR
    # sizing / fixed-stop sizing / Kelly scaling. It complements
    # `max_ticker_allocation_pct` (percentage cap) and
    # `max_sector_concentration_pct` (sector cap).
    max_shares_per_position: int = Field(default=50, ge=1, le=100000)
    # V3.1: Circuit breaker — halt trading on consecutive losses
    max_consecutive_losses: int = Field(default=5, ge=0)
    # V3.1: Circuit breaker — halt on max drawdown (% from peak)
    enable_drawdown_circuit_breaker: bool = True
    # V3.2: Ticker re-entry cooldown (minutes) — blocks re-entering a
    # ticker after exit to prevent whipsawing. 0 disables the feature.
    ticker_reentry_cooldown_minutes: int = Field(default=30, ge=0)
    # V3.2: Position size multiplier for YELLOW (mean-reversion) signals.
    # Reduces position size to limit risk on non-breakout entries.
    yellow_allocation_pct: float = Field(default=0.5, gt=0.0, le=1.0)
    # Phase 1: Optional fractional Kelly sizing overlay.
    # Uses signal confidence as win-probability proxy and scales the
    # baseline risk-model position size down to the Kelly fraction.
    use_kelly_sizing: bool = Field(default=False)
    kelly_fraction_scale: float = Field(default=0.5, gt=0.0, le=1.0)
    kelly_min_position_pct: float = Field(default=0.25, gt=0.0, le=1.0)


class SessionSettings(BaseModel):
    close_hour: int = Field(default=16, ge=0, le=23)
    close_minute: int = Field(default=0, ge=0, le=59)
    eod_minutes_before_close: int = Field(default=5, ge=0, le=120)
    eod_enabled: bool = True
    time_exit_minutes: int = Field(default=0, ge=0, le=480)


class PaperSettings(BaseModel):
    fee_per_order: float = Field(default=1.0, ge=0.0)
    slippage_bps: int = Field(default=0, ge=0)
    dynamic_slippage_enabled: bool = Field(default=False)
    dynamic_slippage_notional_bps_per_10k: float = Field(default=1.0, ge=0.0, le=50.0)
    dynamic_slippage_low_price_boost_bps: float = Field(default=5.0, ge=0.0, le=50.0)
    dynamic_slippage_max_extra_bps: float = Field(default=25.0, ge=0.0, le=100.0)
    partial_take_profit_enabled: bool = Field(default=False)
    partial_take_profit_fraction: float = Field(default=0.5, gt=0.0, lt=1.0)
    partial_take_profit_min_qty: int = Field(default=2, ge=1)


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


class SupermodelSettings(BaseModel):
    support_threshold: float = Field(default=0.72, ge=0.0, le=1.0)
    block_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    counter_veto_weight: float = Field(default=1.0, ge=0.0, le=1.0)


class StrategyTrackerSettings(BaseModel):
    window: int = Field(default=20, ge=1)
    min_win_rate: float = Field(default=0.20, ge=0.0, le=1.0)
    full_allocation_rate: float = Field(default=0.50, ge=0.0, le=1.0)


class EodDataStoreSettings(BaseModel):
    """Settings for the end-of-day data store populated from massive.com S3 flat-files.

    Credentials live in environment variables (``MASSIVE_S3_*``), loaded via
    ``python-dotenv`` in ``trading_bot/main.py``. The settings object exposes
    runtime knobs only — never secrets — to keep the loader's
    "no credentials in config" guard intact.
    """

    enabled: bool = Field(default=True)
    provider: str = Field(default="massive_flat_files")
    intervals: list[str] = Field(default_factory=lambda: ["1d", "1m"])
    # Backfill windows. Starter plan = 5y daily, 1y minute.
    backfill_years: int = Field(default=5, ge=1, le=50)
    minute_backfill_years: int = Field(default=1, ge=0, le=10)
    # Throttle between S3 GETs. Daily batch is O(days) calls so this is light.
    throttle_seconds: float = Field(default=0.2, ge=0.0)
    max_retries: int = Field(default=3, ge=1)
    # Filesystem layout (paths relative to repo root).
    store_root: str = Field(default="state/data_store")
    manifest_db: str = Field(default="state/data_store.db")
    # Region the S3 endpoint uses. Massive.com's S3-compatible layer defaults
    # to us-east-1; override in config if your account is elsewhere.
    s3_region: str = Field(default="us-east-1")
    # TLS verification. Default is strict (verify=True) — safe on the open
    # internet. Set ``verify_tls: false`` only for trusted self-signed
    # endpoints (massive.com's flat-files endpoint as of 2026-07 serves a
    # self-signed cert). The alternative is to pin the endpoint's CA via
    # ``tls_ca_bundle: "/path/to/ca.pem"`` and leave ``verify_tls`` at its
    # True default.
    verify_tls: bool = Field(default=True)
    tls_ca_bundle: str | None = Field(default=None)
    # Auth mode. ``"sigv4"`` (default) signs requests with AWS Signature V4 —
    # works against real AWS S3 and most S3-compatible gateways. ``"bearer"``
    # sends ``Authorization: Bearer <access_key>`` instead — useful for
    # gateways (like massive.com's flat-files endpoint as of 2026-07) that
    # expose S3-like paths but authenticate via REST-API keys.
    auth_mode: str = Field(default="sigv4")
    # S3 addressing style. ``"path"`` (default) — requests go to
    # ``https://<endpoint>/<bucket>/<key>``. Required for massive.com's
    # flat-files gateway as of 2026-07, which routes virtual-hosted requests
    # to its REST API gateway and rejects the SigV4 signature. Set to
    # ``"virtual"`` for real AWS S3 (``https://<bucket>.<endpoint>/<key>``).
    addressing_style: str = Field(default="path")
    # S3 key templates per product. Default templates mirror the public
    # massive.com docs example. For the actually-hosted bucket (as of
    # 2026-07), override to ``us_stocks_sip/day_aggs_v1/{year}/{month}/{date}.csv.gz``
    # etc. Supported placeholders: ``{product}``, ``{date}``, ``{year}``,
    # ``{month}``, ``{day}``. Set to null to use the default template for
    # that product.
    day_aggregates_key_template: str | None = Field(default=None)
    minute_aggregates_key_template: str | None = Field(default=None)
    quotes_key_template: str | None = Field(default=None)
    trades_key_template: str | None = Field(default=None)


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


class SwarmSettings(BaseModel):
    """Multi-agent swarm analysis configuration.

    When ``enabled``, the orchestrator runs a swarm analysis on the
    universe before generating signals. Swarm results are logged
    alongside scanner results. Swarm sentiment can affect position
    sizing when enabled; there is no separate ``signal_mode`` setting.
    """
    enabled: bool = Field(default=False)
    preset: str = Field(default="investment_committee")
    max_workers: int = Field(default=4, ge=1, le=16)
    swarm_weight: float = Field(default=0.3, ge=0.0, le=1.0)


class SentimentSettings(BaseModel):
    """Sentiment/news context configuration.

    Runtime trading stays offline-first: local JSON context is consumed by
    default, while RSS/API fetching must be explicitly enabled by operators.
    """

    enabled: bool = Field(default=True)
    context_path: str = "state/sentiment_context.json"
    rss_feeds: list[str] = Field(default_factory=list)
    fetch_rss: bool = Field(default=False)
    max_items_per_feed: int = Field(default=20, ge=1, le=100)
    memory_enabled: bool = Field(default=False)
    memory_db_path: str = "state/memory.db"


class Settings(BaseModel):
    app: AppSettings = Field(default_factory=AppSettings)
    market_data: MarketDataSettings = Field(default_factory=MarketDataSettings)
    scout: ScoutSettings = Field(default_factory=ScoutSettings)
    advisory: AdvisorySettings = Field(default_factory=AdvisorySettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    paper: PaperSettings = Field(default_factory=PaperSettings)
    alerts: AlertsSettings = Field(default_factory=AlertsSettings)
    robinhood: RobinhoodSettings = Field(default_factory=RobinhoodSettings)
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    supermodel: SupermodelSettings = Field(default_factory=SupermodelSettings)
    strategy_tracker: StrategyTrackerSettings = Field(default_factory=StrategyTrackerSettings)
    eod_data_store: EodDataStoreSettings = Field(default_factory=EodDataStoreSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    counter_thesis: CounterThesisSettings = Field(default_factory=CounterThesisSettings)
    rl: dict[str, object] = Field(default_factory=dict)
    swarm: SwarmSettings = Field(default_factory=SwarmSettings)
    sentiment: SentimentSettings = Field(default_factory=SentimentSettings)

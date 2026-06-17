from pydantic import BaseModel, Field


class AppSettings(BaseModel):
    live_trading_enabled: bool = False
    timezone: str = "America/New_York"
    state_db_path: str = "state/trading_bot.db"
    log_dir: str = "logs"
    dashboard_summary_path: str = "state/dashboard_summary.json"
    scan_results_path: str = "state/scan_results.json"
    portfolio_summary_path: str = "state/portfolio_summary.json"
    backtest_summary_path: str = "state/backtest_summary.json"


class MarketDataSettings(BaseModel):
    provider: str = "yfinance"
    daily_period: str = "1y"
    intraday_period: str = "5d"
    intraday_interval: str = "5m"


class RiskSettings(BaseModel):
    max_risk_per_trade_pct: float = Field(default=0.01, gt=0.0, le=1.0)
    max_daily_risk_pct: float = Field(default=0.03, gt=0.0, le=1.0)
    max_daily_orders: int = Field(default=3, ge=1)
    max_ticker_allocation_pct: float = Field(default=0.20, gt=0.0, le=1.0)
    min_reward_risk_ratio: float = Field(default=2.0, gt=0.0)


class Settings(BaseModel):
    app: AppSettings = Field(default_factory=AppSettings)
    market_data: MarketDataSettings = Field(default_factory=MarketDataSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)

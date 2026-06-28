"""
Application settings and configuration management.

Uses pydantic-settings for environment variable loading with validation.
Includes cross-field validation to ensure configuration consistency.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PrivateAttr, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Cross-field validation warnings collected by validate_configuration().
    # Populated rather than raised so invalid config degrades instead of failing
    # startup; tooling (e.g. check_config.py) can surface these to the operator.
    _config_warnings: list[str] = PrivateAttr(default_factory=list)

    @property
    def config_warnings(self) -> list[str]:
        """Cross-field configuration warnings detected at load time (may be empty)."""
        return self._config_warnings

    # ===========================================
    # LLM Provider - Groq
    # ===========================================
    groq_api_key: SecretStr = Field(..., description="Groq API key for LLM access")
    groq_model_primary: str = Field(
        default="llama-3.3-70b-versatile",
        description="Primary Groq model for agent reasoning",
    )
    groq_model_fallback: str = Field(
        default="llama-3.1-8b-instant",
        description="Fallback Groq model for rate limit scenarios",
    )
    groq_temperature: float = Field(
        default=0.1,
        description="Temperature for LLM responses (low for consistency)",
    )
    groq_max_tokens: int = Field(
        default=2048,
        description="Maximum tokens per LLM response",
    )

    # ===========================================
    # Broker API - DhanHQ (Optional for free tier)
    # ===========================================
    dhan_client_id: str | None = Field(
        default=None,
        description="DhanHQ client ID (optional for local paper trading)",
    )
    dhan_access_token: SecretStr | None = Field(
        default=None,
        description="DhanHQ access token (optional for local paper trading)",
    )
    dhan_base_url: str = Field(
        default="https://api.dhan.co/v2",
        description="DhanHQ API base URL (use https://api.dhan.co/v2 for live)",
    )

    # ===========================================
    # Observability - LangSmith
    # ===========================================
    langsmith_api_key: SecretStr = Field(..., description="LangSmith API key")
    langsmith_project: str = Field(
        default="trading-agent",
        description="LangSmith project name for tracing",
    )
    langsmith_tracing_v2: bool = Field(
        default=True,
        description="Enable LangSmith tracing v2",
    )

    # ===========================================
    # Database - PostgreSQL
    # ===========================================
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/trading_agent",
        description="PostgreSQL connection URL",
    )

    # ===========================================
    # Optional: Redis (not required for basic functionality)
    # ===========================================
    redis_url: str | None = Field(
        default=None,
        description="Optional Redis URL for market data caching",
    )

    # ===========================================
    # Trading Configuration
    # ===========================================
    trading_mode: Literal["paper", "live"] = Field(
        default="paper",
        description="Trading mode - paper for simulation, live for real trading",
    )
    max_daily_trades: int = Field(
        default=50,
        description="Maximum number of trades allowed per day",
    )
    max_position_size: float = Field(
        default=100000.0,
        description="Maximum position size in INR",
    )
    daily_loss_limit: float = Field(
        default=10000.0,
        description="Maximum daily loss limit in INR (kill switch trigger)",
    )

    # ===========================================
    # Agent Memory Configuration
    # ===========================================
    memory_top_n_lessons: int = Field(
        default=5,
        description="Number of top lessons to inject into agent context",
    )
    memory_decay_days: int = Field(
        default=30,
        description="Days after which lesson relevance starts decaying",
    )

    # ===========================================
    # Free Tier Configuration
    # ===========================================
    market_data_source: Literal["yfinance", "dhan"] = Field(
        default="yfinance",
        description="Market data source: yfinance (free) or dhan (requires account)",
    )
    execution_mode: Literal["local_paper", "dhan_paper", "live"] = Field(
        default="local_paper",
        description="Execution mode: local_paper (free), dhan_paper (sandbox), or live",
    )
    enable_news_analysis: bool = Field(
        default=True,
        description="Enable AI-powered news sentiment analysis",
    )
    paper_wallet_balance: float = Field(
        default=1000000.0,
        description="Starting balance for local paper trading (INR)",
    )

    # ===========================================
    # Telegram Notifications
    # ===========================================
    telegram_bot_token: str | None = Field(
        default=None,
        description="Telegram bot token from @BotFather",
    )
    telegram_chat_id: str | None = Field(
        default=None,
        description="Your Telegram chat ID from @userinfobot",
    )
    telegram_enabled: bool = Field(
        default=True,
        description="Enable Telegram notifications",
    )

    # ===========================================
    # Market Hours Configuration
    # ===========================================
    market_open_time: str = Field(
        default="09:15",
        description="Market open time (HH:MM) in IST",
    )
    market_close_time: str = Field(
        default="15:30",
        description="Market close time (HH:MM) in IST",
    )
    no_trading_before: str = Field(
        default="09:15",
        description="No trading before this time (HH:MM)",
    )
    no_trading_after: str = Field(
        default="15:15",
        description="No trading after this time (HH:MM)",
    )

    # ===========================================
    # Position Sizing Configuration
    # ===========================================
    max_position_pct: float = Field(
        default=0.10,
        description="Maximum position size as fraction of capital (0.10 = 10%)",
    )
    risk_per_trade: float = Field(
        default=0.02,
        description="Maximum risk per trade as fraction of capital (0.02 = 2%)",
    )
    max_total_risk: float = Field(
        default=0.10,
        description="Maximum total portfolio risk (0.10 = 10%)",
    )
    max_sector_exposure: float = Field(
        default=0.30,
        description="Maximum exposure to single sector (0.30 = 30%)",
    )

    # ===========================================
    # Rate Limiting Configuration
    # ===========================================
    groq_requests_per_minute: int = Field(
        default=30,
        description="Groq API rate limit (requests per minute)",
    )
    enable_rate_limiting: bool = Field(
        default=True,
        description="Enable rate limiting for API calls",
    )

    # ===========================================
    # Circuit Breaker Configuration
    # ===========================================
    circuit_breaker_failure_threshold: int = Field(
        default=5,
        description="Failures before circuit breaker opens",
    )
    circuit_breaker_recovery_time: float = Field(
        default=60.0,
        description="Seconds before circuit breaker attempts recovery",
    )

    # ===========================================
    # Cache Configuration
    # ===========================================
    cache_news_ttl: int = Field(
        default=300,
        description="News cache TTL in seconds (5 minutes)",
    )
    cache_quotes_ttl: int = Field(
        default=60,
        description="Quote cache TTL in seconds (1 minute)",
    )
    cache_sentiment_ttl: int = Field(
        default=600,
        description="Sentiment cache TTL in seconds (10 minutes)",
    )

    # ===========================================
    # FinOps - Cost tracking & budgets
    # ===========================================
    finops_enabled: bool = Field(
        default=True,
        description="Enable LLM cost/token accounting and budget alerts",
    )
    daily_token_budget: int = Field(
        default=0,
        description="Max Groq tokens per IST day across all agents (0 = unlimited)",
    )
    daily_cost_budget_usd: float = Field(
        default=0.0,
        description="Max paid-tier-equivalent LLM spend per IST day in USD (0 = unlimited)",
    )
    finops_budget_soft_pct: float = Field(
        default=0.8,
        description="Soft-alert threshold as a fraction of a daily budget (0.8 = 80%)",
    )

    # ===========================================
    # Profit-target goal engine
    # ===========================================
    monthly_profit_target_pct: float = Field(
        default=0.0,
        description="Monthly profit target as a fraction of capital (0.05 = 5%/mo; 0 = disabled)",
    )
    monthly_profit_target_amount: float = Field(
        default=0.0,
        description="Monthly profit target in INR (alternative to pct; the larger of the two wins)",
    )
    trading_days_per_month: int = Field(
        default=21,
        description="Assumed NSE trading days per month, used to derive the daily pace target",
    )
    expected_trades_per_day: int = Field(
        default=5,
        description="Expected trades/day, used to derive the win-rate the target requires",
    )
    goal_assumed_win_rate: float = Field(
        default=0.5,
        description="Assumed win rate, used to derive the trade frequency the target requires",
    )
    goal_reward_risk_ratio: float = Field(
        default=1.5,
        description="Assumed reward:risk per trade for goal math (matches risk min_risk_reward)",
    )
    goal_off_pace_tolerance: float = Field(
        default=0.2,
        description="How far below straight-line pace before flagged off-pace (0.2 = 20%)",
    )

    # ===========================================
    # Validation
    # ===========================================

    @model_validator(mode='after')
    def validate_configuration(self) -> "Settings":
        """Validate configuration consistency."""
        errors = []
        
        # Live trading requires broker credentials
        if self.trading_mode == "live":
            if not self.dhan_client_id or not self.dhan_access_token:
                errors.append("Live trading requires Dhan credentials (DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN)")
        
        # Dhan execution modes require Dhan data source
        if self.execution_mode in ["dhan_paper", "live"] and self.market_data_source == "yfinance":
            errors.append("Dhan execution modes should use 'dhan' market_data_source for consistency")
        
        # Validate risk parameters
        if self.risk_per_trade > self.max_position_pct:
            errors.append(f"risk_per_trade ({self.risk_per_trade}) should not exceed max_position_pct ({self.max_position_pct})")
        
        if self.max_total_risk < self.risk_per_trade:
            errors.append(f"max_total_risk ({self.max_total_risk}) should not be less than risk_per_trade ({self.risk_per_trade})")
        
        # Validate market hours
        try:
            from datetime import datetime
            open_time = datetime.strptime(self.market_open_time, "%H:%M")
            close_time = datetime.strptime(self.market_close_time, "%H:%M")
            if open_time >= close_time:
                errors.append("market_open_time must be before market_close_time")
        except ValueError as e:
            errors.append(f"Invalid market hours format: {e}")
        
        # Validate trading window
        try:
            from datetime import datetime
            no_before = datetime.strptime(self.no_trading_before, "%H:%M")
            no_after = datetime.strptime(self.no_trading_after, "%H:%M")
            if no_before >= no_after:
                errors.append("no_trading_before must be before no_trading_after")
        except ValueError as e:
            errors.append(f"Invalid trading window format: {e}")
        
        # Telegram requires both token and chat_id
        if self.telegram_enabled:
            if self.telegram_bot_token and not self.telegram_chat_id:
                errors.append("telegram_chat_id required when telegram_bot_token is set")
            if self.telegram_chat_id and not self.telegram_bot_token:
                errors.append("telegram_bot_token required when telegram_chat_id is set")
        
        # Store warnings on the instance so tooling can surface them, and log them.
        self._config_warnings = errors
        if errors:
            # Log warnings instead of raising for non-critical issues
            import logging
            logger = logging.getLogger(__name__)
            for error in errors:
                logger.warning(f"Configuration warning: {error}")

        return self


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()


def reload_settings() -> Settings:
    """Reload settings (clears cache)."""
    get_settings.cache_clear()
    return get_settings()

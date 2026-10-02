"""
Application settings and configuration management.

Uses pydantic-settings for environment variable loading with validation.
Includes cross-field validation to ensure configuration consistency.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    Field,
    PrivateAttr,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchors for files the app owns. Never resolve these against the working directory.
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE_OVERRIDE = "RAKSHAQUANT_ENV_FILE"  # a path, or "none" to read no .env at all


def _env_file() -> Path | None:
    override = os.environ.get(ENV_FILE_OVERRIDE)
    if override is None:
        return REPO_ROOT / ".env"
    if override.strip().lower() in ("", "none"):
        return None
    path = Path(override).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


ENV_FILE = _env_file()

Environment = Literal["dev", "paper", "demo", "test"]


def _repo_path(value: Any) -> Path:
    """An absolute path; relative values are anchored at the repo root, never the CWD."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
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
    # Environment & runtime state
    # ===========================================
    environment: Environment = Field(
        default="dev",
        description="Runtime environment; selects the state directory. The month run uses "
        "'paper'; 'demo' is for simulated/replayed data; 'test' is for the test suite.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Level for the JSON log file under var/logs/",
    )
    var_dir: Path = Field(
        default=None,
        validate_default=True,
        description="Root of all runtime files (default <repo>/var): per-environment state plus "
        "the shared tape/, logs/, reports/, reference/, models/, datasets/ and archive/.",
    )
    state_dir: Path = Field(
        default=None,
        validate_default=True,
        description="Absolute directory for this environment's state (default "
        "<var_dir>/<environment>). Relative values resolve against the repo root, never the CWD.",
    )

    @field_validator("var_dir", mode="before")
    @classmethod
    def _resolve_var_dir(cls, value: Any) -> Path:
        if value is None or value == "":
            return REPO_ROOT / "var"
        return _repo_path(value)

    @field_validator("state_dir", mode="before")
    @classmethod
    def _resolve_state_dir(cls, value: Any, info: ValidationInfo) -> Path:
        if value is None or value == "":
            var_dir = info.data.get("var_dir", REPO_ROOT / "var")
            return Path(var_dir) / str(info.data.get("environment", "dev"))
        return _repo_path(value)

    @property
    def db_path(self) -> Path:
        """The environment's SQLite event store."""
        return self.state_dir / "rakshaquant.db"

    @property
    def halt_file(self) -> Path:
        """Create this file to trip the global kill switch (content ``FLATTEN`` to flatten)."""
        return self.state_dir / "HALT"

    @property
    def tape_dir(self) -> Path:
        return self.var_dir / "tape"

    @property
    def logs_dir(self) -> Path:
        return self.var_dir / "logs"

    @property
    def reports_dir(self) -> Path:
        return self.var_dir / "reports"

    @property
    def reference_dir(self) -> Path:
        return self.var_dir / "reference"

    @property
    def models_dir(self) -> Path:
        return self.var_dir / "models"

    @property
    def datasets_dir(self) -> Path:
        return self.var_dir / "datasets"

    @property
    def archive_dir(self) -> Path:
        return self.var_dir / "archive"

    # ===========================================
    # LLM provider keys (plan M6): set only the providers your roles use
    # ===========================================
    groq_api_key: SecretStr | None = Field(
        default=None, description="Groq API key (optional: only for roles that use groq:)"
    )
    openai_api_key: SecretStr | None = Field(default=None, description="OpenAI API key")
    openrouter_api_key: SecretStr | None = Field(default=None, description="OpenRouter API key")
    anthropic_api_key: SecretStr | None = Field(default=None, description="Anthropic API key")
    ollama_base_url: str = Field(
        default="http://localhost:11434/v1", description="Ollama's OpenAI-compatible endpoint"
    )
    llm_compat_base_url: str | None = Field(
        default=None, description="Any other OpenAI-compatible endpoint (provider 'compat')"
    )
    llm_compat_api_key: SecretStr | None = Field(default=None, description="Key for 'compat'")
    llm_timeout_s: float = Field(default=20.0, gt=0, le=120, description="Per LLM call")
    llm_breaker_failures: int = Field(
        default=3, ge=1, le=50, description="Consecutive failures that open a model's breaker"
    )
    llm_breaker_cooldown_s: float = Field(
        default=120.0, gt=0, le=3600, description="How long an open breaker skips the model"
    )
    llm_cache_enabled: bool = Field(
        default=True, description="Reuse a stored reply for an identical prompt and model"
    )
    # Roles: "provider:model" (empty = role disabled), comma-separated fallbacks, optional effort.
    llm_role_veto: str = Field(default="", description="Book C veto (online, entry window)")
    llm_role_veto_fallbacks: str = ""
    llm_role_veto_effort: str | None = None
    llm_role_review: str = Field(default="", description="Nightly post-trade review (offline)")
    llm_role_review_fallbacks: str = ""
    llm_role_review_effort: str | None = None
    llm_role_explain: str = Field(default="", description="Trade explanations (offline)")
    llm_role_explain_fallbacks: str = ""
    llm_role_explain_effort: str | None = None
    llm_role_label: str = Field(default="", description="Teacher labels (offline, batch)")
    llm_role_label_fallbacks: str = ""
    llm_role_label_effort: str | None = None
    llm_role_research: str = Field(default="", description="Weekly research memo (offline)")
    llm_role_research_fallbacks: str = ""
    llm_role_research_effort: str | None = None
    usd_inr: float = Field(default=88.0, gt=0, description="USD->INR for LLM cost accounting")
    llm_budget_daily_inr: float = Field(
        default=200.0, ge=0, description="All roles, per IST day (0 = unlimited)"
    )
    llm_budget_role_daily_inr: dict[str, float] = Field(
        default_factory=dict, description='Per role, per IST day, e.g. {"veto": 50}'
    )
    llm_budget_per_decision_inr: float = Field(
        default=10.0, ge=0, description="Per decision_id (0 = unlimited)"
    )
    llm_openrouter_referer: str = Field(
        default="", description="Optional HTTP-Referer sent to OpenRouter (app attribution)"
    )
    llm_openrouter_deny_data_collection: bool = Field(
        default=True, description="Ask OpenRouter to route only to no-data-retention providers"
    )
    llm_anthropic_fallbacks: bool = Field(
        default=True, description="Anthropic server-side refusal fallbacks (where supported)"
    )

    # ===========================================
    # Corporate announcements (plan M7.6)
    # ===========================================
    announcements_enabled: bool = Field(
        default=True, description="Poll NSE's announcements RSS feed during the session"
    )
    announcements_url: str = Field(
        default="https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
        description="The RSS feed (polled no faster than its <ttl>)",
    )

    # ===========================================
    # Typed decision models (plan M7): Laya (local) -> Jev (TypeSafe)
    # ===========================================
    decision_laya_enabled: bool = Field(
        default=True, description="Use local Laya when the decision-local extra is installed"
    )
    decision_laya_checkpoint: Literal["english", "multilingual", "typed-decisions"] = Field(
        default="multilingual", description="Fastest on CPU (docs/plan/decision-model-benchmark.md)"
    )
    typesafe_api_key: SecretStr | None = Field(default=None, description="Jev (TypeSafe) key")
    typesafe_model: str = Field(default="jev-1.13.0", description="Pinned Jev model")
    decision_escalate_low: float = Field(default=0.35, ge=0, le=1)
    decision_escalate_high: float = Field(default=0.65, ge=0, le=1)
    infra_cost_inr_per_day: float | None = Field(
        default=None, ge=0, description="Power, data and API plans per day, for the daily report"
    )
    experiment_file: Path | None = Field(
        default=None, description="The experiment definition (default src/config/experiment.yaml)"
    )
    decision_shadow_pct: float = Field(
        default=0.20,
        ge=0,
        le=1,
        description="Laya-only states also sent to Jev to measure agreement",
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
    # Execution venue
    # ===========================================
    execution_mode: Literal["local_paper", "shadow", "dhan_paper", "live"] = Field(
        default="local_paper",
        description="The requested venue. The v2 engine trades on the simulated broker only: "
        "anything but local_paper/shadow is ignored, with a warning.",
    )

    # ===========================================
    # Telegram Notifications
    # ===========================================
    telegram_bot_token: SecretStr | None = Field(
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
    # Validation
    # ===========================================

    @model_validator(mode="after")
    def validate_configuration(self) -> "Settings":
        """Validate configuration consistency."""
        errors = []

        # The v2 engine has no broker path: a broker venue is ignored, never honoured
        if self.execution_mode not in ("local_paper", "shadow"):
            errors.append(
                f"EXECUTION_MODE={self.execution_mode} is ignored: the v2 engine trades on the "
                "simulated broker only (paper)"
            )

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

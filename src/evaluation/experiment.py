"""
The experiment definition (plan M8.1): ``src/config/experiment.yaml`` → a validated
:class:`ExperimentConfig`, and from it the engine's book list, capital, entry window, policy and
strategies. Month 1 is CNC long-only with learning injection off; anything else fails startup.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.config.errors import ConfigError

DEFAULT_EXPERIMENT_PATH = Path(__file__).resolve().parents[1] / "config" / "experiment.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BookSpec(_Strict):
    advisor: Literal["none", "typed_veto", "llm_veto"]
    threshold: float | None = Field(default=None, gt=0, le=1)
    role: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> BookSpec:
        if self.advisor == "llm_veto" and not self.role:
            raise ValueError("an llm_veto book needs the LLM role it uses (e.g. role: veto)")
        if self.advisor == "none" and (self.threshold is not None or self.role):
            raise ValueError("a book without an advisor takes no threshold or role")
        return self


class Strategies(_Strict):
    enabled: tuple[str, ...]
    shadow: tuple[str, ...] = ()


class PolicySpec(_Strict):
    k_stop_atr: float = 2.0
    k_target_atr: float = 3.0
    max_hold_days: int = 10
    partial_at_r: float | None = None


class ExperimentConfig(_Strict):
    experiment: str
    universe: Literal["nifty50"]
    capital_inr: Decimal = Field(gt=0)
    product: Literal["CNC"]
    direction: Literal["long_only"]
    entry_window: str
    strategies: Strategies
    policy: PolicySpec = PolicySpec()
    books: dict[str, BookSpec]
    benchmark: str = "^NSEI"
    learning_injection: Literal[False] = False

    @model_validator(mode="after")
    def _books(self) -> ExperimentConfig:
        if not self.books:
            raise ValueError("an experiment needs at least one book")
        for book_id in self.books:
            if not book_id.isalnum():
                raise ValueError(f"book id {book_id!r} must be alphanumeric")
        if set(self.strategies.enabled) & set(self.strategies.shadow):
            raise ValueError("a strategy cannot be both enabled and shadow")
        self.window()
        return self

    def window(self) -> tuple[time, time]:
        start, sep, end = self.entry_window.partition("-")
        try:
            begin, finish = time.fromisoformat(start.strip()), time.fromisoformat(end.strip())
        except ValueError as exc:
            raise ValueError(
                f"entry_window {self.entry_window!r} must look like 09:20-09:45"
            ) from exc
        if not sep or not begin < finish:
            raise ValueError(f"entry_window {self.entry_window!r} must look like 09:20-09:45")
        return begin, finish

    @property
    def book_ids(self) -> tuple[str, ...]:
        return tuple(self.books)


def load_experiment(path: Path = DEFAULT_EXPERIMENT_PATH) -> ExperimentConfig:
    """Load and validate the experiment; any problem is a ``ConfigError`` (exit 2)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ExperimentConfig.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"experiment config {path.name}: {exc}") from exc

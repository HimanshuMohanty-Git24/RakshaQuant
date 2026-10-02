"""Building the decision-model cascade from the settings (plan M7): Laya when the optional
``decision-local`` extra is installed and enabled, Jev when ``TYPESAFE_API_KEY`` is set. With
neither, there is no cascade and callers degrade (announcements are stored, not classified)."""

from __future__ import annotations

import importlib.util
from typing import Any

from src.decision_models.adapters import LayaLocal, jev_remote
from src.decision_models.base import DecisionModel
from src.decision_models.cascade import Calibrator, Cascade, CascadeConfig
from src.domain.sink import EventSink


def laya_available() -> bool:
    return importlib.util.find_spec("laya") is not None


def build_cascade(
    settings: Any, *, sink: EventSink, calibrator: Calibrator | None = None
) -> Cascade | None:
    laya: DecisionModel | None = None
    if settings.decision_laya_enabled and laya_available():
        laya = LayaLocal(checkpoint=settings.decision_laya_checkpoint,
                         cache_dir=str(settings.models_dir / "hf"))  # fmt: skip
    jev: DecisionModel | None = None
    key = settings.typesafe_api_key
    if key is not None and key.get_secret_value():
        jev = jev_remote(key.get_secret_value(), model=settings.typesafe_model)
    if laya is None and jev is None:
        return None
    config = CascadeConfig(
        escalate_band=(settings.decision_escalate_low, settings.decision_escalate_high),
        shadow_pct=settings.decision_shadow_pct,
    )
    return Cascade(laya=laya, jev=jev, sink=sink, config=config, calibrator=calibrator)

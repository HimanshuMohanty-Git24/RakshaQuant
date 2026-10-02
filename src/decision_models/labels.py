"""
The labelled announcement set for decision-model calibration (plan M7.5).

1. **Collect** announcements: those our RSS reader stored (``AnnouncementReceived`` events), and a
   CSV the owner downloads by hand from NSE's corporate-announcements page (the RSS holds only
   the latest items, and NSE's terms rule out scraping its announcement archive - OQ-2).
2. **Teacher-label** them with the LLM role ``label`` (prompt ``label_announcement_v1``) through
   the router - paid calls, so the script runs them only with an explicit confirmation and
   prints the estimated cost first.
3. **Spot-check**: export a random sample to CSV for the owner; **merge** the corrections back
   (a corrected row is the ground truth).
4. **Predict**: run a decision model over the labelled rows to fit and score calibration.

The dataset lives at ``<var>/datasets/announcements_labels.parquet``.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from src.decision_models.base import Answer, DecisionModel, DecisionModelError
from src.decision_models.tasks.announcements import (
    EVENT_TYPES,
    MATERIALITY,
    QUESTIONS,
    announcement_state,
)
from src.domain.events import AnnouncementReceived
from src.domain.types import Instrument
from src.llm.pricing import PricingTable, to_inr
from src.llm.prompts.templates import LABEL_ANNOUNCEMENT_V1, AnnouncementLabel
from src.llm.registry import ModelSpec
from src.llm.router import LLMRouter
from src.llm.types import Usage
from src.marketdata.announcements import CompanyMatcher, announcement_id
from src.utils.market_time import IST

logger = logging.getLogger(__name__)

TASK = "announcements"
COLUMNS = (
    "announcement_id", "instrument_key", "company", "published_at", "title", "subject",
    "label_relevant", "label_event_type", "label_direction", "label_materiality",
    "label_confidence", "teacher_model", "prompt_version", "corrected",
)  # fmt: skip
LABEL_ORDER: Mapping[str, tuple[str, ...]] = {
    "relevant": ("false", "true"),
    "event_type": tuple(EVENT_TYPES),
    "direction": ("positive", "negative", "neutral", "unclear"),
    "materiality": MATERIALITY,
}
# NSE's corporate-announcements CSV export, tolerant of naming (lower-cased headers).
_CSV_COLUMNS = {
    "symbol": ("symbol",),
    "company": ("company name", "company", "sm_name"),
    "subject": ("subject", "desc"),
    "details": ("details", "attchmnttext", "description", "text"),
    "date": ("broadcast date/time", "an_dt", "broadcast date", "date", "dissemination"),
}


def empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})


def from_announcements(items: Iterable[AnnouncementReceived]) -> pd.DataFrame:
    rows = [{"announcement_id": a.announcement_id, "instrument_key": a.instrument_key,
             "company": a.company, "published_at": a.published_at.isoformat(), "title": a.title,
             "subject": a.subject} for a in items]  # fmt: skip
    return _normalise(pd.DataFrame(rows))


def from_nse_csv(path: Path, instruments: Iterable[Instrument]) -> pd.DataFrame:
    """Rows of an owner-downloaded NSE announcements CSV that belong to the universe."""
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    lower = {c.strip().lower(): c for c in raw.columns}
    found = {name: next((lower[a] for a in aliases if a in lower), None)
             for name, aliases in _CSV_COLUMNS.items()}  # fmt: skip
    if found["details"] is None or found["date"] is None:
        raise ValueError(f"{path.name}: needs a details/text column and a date column")
    by_symbol = {i.symbol: i for i in instruments}
    matcher = CompanyMatcher(by_symbol.values())
    rows = []
    for record in raw.to_dict("records"):
        symbol = str(record.get(found["symbol"] or "", "")).strip()
        company = str(record.get(found["company"] or "", "")).strip()
        instrument = by_symbol.get(symbol) or (matcher.match(company) if company else None)
        published = _parse_csv_ts(str(record[found["date"]]))
        text = " ".join(str(record[found["details"]]).split())
        if instrument is None or published is None or not text:
            continue
        rows.append({"announcement_id": announcement_id(instrument.key, published, text),
                     "instrument_key": instrument.key, "company": company or instrument.name,
                     "published_at": published.isoformat(), "title": text[:2000],
                     "subject": str(record.get(found["subject"] or "", "")).strip()})  # fmt: skip
    return _normalise(pd.DataFrame(rows))


def _parse_csv_ts(text: str) -> datetime | None:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    out = empty() if df.empty else df.copy()
    for column in COLUMNS:
        if column not in out.columns:
            out[column] = False if column == "corrected" else None
    out["corrected"] = out["corrected"].fillna(False).astype(bool)
    return out[list(COLUMNS)].reset_index(drop=True)


def merge(*frames: pd.DataFrame) -> pd.DataFrame:
    """Union by ``announcement_id``; an existing (labelled) row wins over a new copy."""
    parts = [f for f in frames if not f.empty]
    if not parts:
        return empty()
    combined = pd.concat(parts, ignore_index=True)
    combined["_labelled"] = combined["label_event_type"].notna()
    combined = combined.sort_values("_labelled", ascending=False, kind="stable")
    combined = combined.drop_duplicates("announcement_id").drop(columns="_labelled")
    return _normalise(combined.sort_values("published_at", kind="stable"))


def load(path: Path) -> pd.DataFrame:
    return _normalise(pd.read_parquet(path)) if path.exists() else empty()


def save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    _normalise(df).to_parquet(tmp, index=False)
    tmp.replace(path)


# -- teacher labels ------------------------------------------------------------------------------


def _label_input(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"announcement": {"company": row["company"], "subject": row["subject"] or "",
                             "text": row["title"]}}  # fmt: skip


def estimate_cost(
    df: pd.DataFrame,
    model: ModelSpec,
    pricing: PricingTable,
    usd_inr: float,
    output_tokens: int = 150,
) -> tuple[int, Decimal | None]:
    """How many teacher calls the unlabelled rows need, and their estimated cost in INR."""
    todo = df[df["label_event_type"].isna()]
    total = Decimal(0)
    for record in todo.to_dict("records"):
        rendered = "".join(m.content for m in LABEL_ANNOUNCEMENT_V1.render(_label_input(record)))
        cost = pricing.cost_usd(model, Usage(input_tokens=len(rendered) // 4 + 50,
                                             output_tokens=output_tokens))  # fmt: skip
        if cost is None:
            return len(todo), None
        total += cost
    return len(todo), to_inr(total, usd_inr)


async def teacher_label(
    df: pd.DataFrame, router: LLMRouter, *, limit: int | None = None
) -> pd.DataFrame:
    """Label the unlabelled rows (at most ``limit``) with the ``label`` role; failures stay
    unlabelled and are retried on the next run."""
    out = df.copy()
    done = 0
    for index, record in out[out["label_event_type"].isna()].iterrows():
        if limit is not None and done >= limit:
            break
        result = await router.complete(
            "label", LABEL_ANNOUNCEMENT_V1.render(_label_input(record)), AnnouncementLabel,
            prompt_version=LABEL_ANNOUNCEMENT_V1.prompt_version,
        )  # fmt: skip
        done += 1
        if not result.ok or result.parsed is None:
            logger.warning("no teacher label for %s: %s", record["announcement_id"], result.outcome)
            continue
        label = result.parsed
        out.loc[index, ["label_relevant", "label_event_type", "label_direction",
                        "label_materiality", "label_confidence", "teacher_model",
                        "prompt_version"]] = [
            label.relevant, label.event_type, label.direction, label.materiality,
            label.confidence, result.model, LABEL_ANNOUNCEMENT_V1.prompt_version,
        ]  # fmt: skip
    return _normalise(out)


# -- owner spot-check ----------------------------------------------------------------------------

CORRECTION_COLUMNS = ("relevant", "event_type", "direction", "materiality")


def spotcheck(df: pd.DataFrame, n: int = 50, seed: int = 7) -> pd.DataFrame:
    labelled = df[df["label_event_type"].notna()]
    sample = labelled.sample(n=min(n, len(labelled)), random_state=seed)
    out = sample[["announcement_id", "company", "subject", "title", "label_relevant",
                  "label_event_type", "label_direction", "label_materiality"]].copy()  # fmt: skip
    for column in CORRECTION_COLUMNS:
        out[f"correct_{column}"] = ""  # the owner fills these where the label is wrong
    return out.reset_index(drop=True)


def merge_corrections(df: pd.DataFrame, corrections: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Apply the owner's non-empty ``correct_*`` cells; returns the dataset and rows changed."""
    out = df.set_index("announcement_id", drop=False)
    valid = {"relevant": {"true", "false"}, "event_type": set(EVENT_TYPES),
             "direction": set(LABEL_ORDER["direction"]), "materiality": set(MATERIALITY)}  # fmt: skip
    changed = 0
    for record in corrections.fillna("").to_dict("records"):
        aid = str(record.get("announcement_id", ""))
        if aid not in out.index:
            continue
        touched = False
        for column in CORRECTION_COLUMNS:
            value = str(record.get(f"correct_{column}", "")).strip().lower()
            if not value:
                continue
            if value not in valid[column]:
                raise ValueError(f"{aid}: {value!r} is not a valid {column}")
            out.loc[aid, f"label_{column}"] = (value == "true") if column == "relevant" else value
            touched = True
        if touched:
            out.loc[aid, "corrected"] = True
            changed += 1
    return _normalise(out.reset_index(drop=True)), changed


# -- predictions for calibration -----------------------------------------------------------------


def label_index(question: str, value: Any) -> int | None:
    order = LABEL_ORDER[question]
    text = ("true" if value else "false") if question == "relevant" else str(value)
    return order.index(text) if text in order else None


def aligned(question: str, answer: Answer) -> list[float]:
    """The answer's probabilities in :data:`LABEL_ORDER` (level names or indices)."""
    probs = answer.probabilities
    return [float(probs.get(name, probs.get(str(i), 0.0)))
            for i, name in enumerate(LABEL_ORDER[question])]  # fmt: skip


async def predict(
    model: DecisionModel, df: pd.DataFrame, *, context_tokens: int = 1024
) -> dict[str, list[tuple[str, list[float], int]]]:
    """Per question: ``(announcement_id, probabilities, label index)`` for every labelled row the
    model answered."""
    out: dict[str, list[tuple[str, list[float], int]]] = {q: [] for q in QUESTIONS}
    for record in df[df["label_event_type"].notna()].to_dict("records"):
        announcement = AnnouncementReceived(
            announcement_id=record["announcement_id"], instrument_key=record["instrument_key"],
            company=record["company"], published_at=datetime.fromisoformat(record["published_at"]),
            received_at=datetime.fromisoformat(record["published_at"]), title=record["title"],
            subject=record["subject"] or "", source="dataset",
        )  # fmt: skip
        try:
            answers = await model.decide(
                announcement_state(announcement, context_tokens), QUESTIONS
            )
        except DecisionModelError as exc:
            logger.warning("no prediction for %s: %s", record["announcement_id"], exc)
            continue
        for question in QUESTIONS:
            index = label_index(question, record[f"label_{question}"])
            answer = answers.get(question)
            if index is not None and answer is not None and answer.probabilities:
                out[question].append((record["announcement_id"], aligned(question, answer), index))
    return out


def split(ids: Sequence[str], holdout: float = 0.3) -> list[bool]:
    """A deterministic train/held-out split by id hash (True = held out)."""
    return [
        int(hashlib.sha256(i.encode()).hexdigest()[:8], 16) % 1000 < holdout * 1000 for i in ids
    ]

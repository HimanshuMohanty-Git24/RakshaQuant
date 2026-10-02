"""Plan M7.7: announcements → TypedEvent through the cascade; exchange subjects first; dates."""

from __future__ import annotations

import inspect
import typing
from collections.abc import Mapping
from datetime import date, datetime, time
from typing import Any

import pytest

from src.decision_models.base import Answer, Question
from src.decision_models.cascade import Cascade, CascadeConfig
from src.decision_models.tasks.announcements import (
    QUESTIONS,
    AnnouncementPipeline,
    EventClassifier,
    announcement_state,
    extract_event_date,
    subject_type,
    unclassified,
)
from src.domain.clock import ReplayClock
from src.domain.events import AnnouncementReceived
from src.domain.sink import RecordingSink
from src.domain.types import AnnouncementType, EventDirection, Materiality
from src.marketdata.announcements import PollStats
from src.store.event_store import EventStore
from src.store.sink import StoreSink
from src.utils.market_time import IST

T0 = datetime.combine(date(2026, 10, 5), time(9, 30), IST)
A = AnnouncementType


def announcement(
    text: str, subject: str = "General Updates", aid: str = "a1"
) -> AnnouncementReceived:
    return AnnouncementReceived(
        announcement_id=aid, instrument_key="NSE:EQ:INFY", company="Infosys Limited",
        published_at=T0, received_at=T0, title=text, subject=subject, source="nse_rss",
    )  # fmt: skip


class Scripted:
    """A decision model answering with fixed values and confidences."""

    name, checkpoint, context_tokens, device = "laya", "multilingual", 1024, "cpu"

    def __init__(
        self, values: Mapping[str, Any], confidence: float = 0.9, fail: bool = False
    ) -> None:
        self.values, self.confidence, self.fail = dict(values), confidence, fail
        self.states: list[Mapping[str, str]] = []

    async def decide(self, state: Mapping[str, str], questions: Mapping[str, Question]):
        self.states.append(dict(state))
        if self.fail:
            from src.decision_models.base import DecisionModelError

            raise DecisionModelError("down")
        out = {}
        for name, q in questions.items():
            value = self.values.get(name)
            probs: dict[str, float] = {}
            if q.type == "score":
                probs = {"minor": 0.1, "moderate": 0.2, "major": 0.7} if value == "major" else {}
            out[name] = Answer(q.type, value, probs, self.confidence, model="laya:multilingual",
                               calibrated=True)  # fmt: skip
        return out


def classifier(model: Scripted | None, jev: Scripted | None = None, sink=None):
    clock = ReplayClock(T0)
    sink = sink or RecordingSink(clock)
    cascade = Cascade(laya=model, jev=jev, sink=sink, config=CascadeConfig(shadow_pct=0))
    return EventClassifier(cascade=cascade, sink=sink, clock=clock), sink


LITIGATION = {"relevant": True, "event_type": "litigation_or_regulatory",
              "direction": "negative", "materiality": "major"}  # fmt: skip


async def test_a_model_classified_event():
    clf, sink = classifier(Scripted(LITIGATION))
    event = await clf.classify(
        announcement("SEBI passed an order imposing a penalty of Rs 50 crore")
    )
    assert (event.relevant, event.announcement_type, event.direction, event.materiality) == (
        True, A.LITIGATION_OR_REGULATORY, EventDirection.NEGATIVE, Materiality.MAJOR,
    )  # fmt: skip
    assert event.model == "laya" and event.calibrated and event.extra["type_source"] == "model"
    assert event.event_id == "a1" and event.published_at == T0
    assert [e.type for e in sink.events] == ["DecisionModelCall", "TypedEvent"]


async def test_a_results_board_meeting_is_typed_by_the_exchange_subject_with_its_date():
    clf, _ = classifier(Scripted({**LITIGATION, "relevant": False, "event_type": "other"}))
    event = await clf.classify(announcement(
        "Infosys Limited has informed the Exchange that the Board of Directors will meet on "
        "October 16, 2026 to consider the financial results for the quarter ended September 30, 2026.",
        subject="Board Meeting Intimation",
    ))  # fmt: skip
    assert event.announcement_type is A.RESULTS_DATE and event.relevant  # despite the model
    assert event.extra["event_date"] == "2026-10-16"
    assert event.extra["type_source"] == "exchange_subject" and event.model == "laya+rules"


@pytest.mark.parametrize(
    ("subject", "text", "expected"),
    [("Financial Result Updates", "Results for Q2", A.RESULTS),
     ("Integrated Filing- Financials", "quarterly filing", A.RESULTS),
     ("Board Meeting Intimation", "to consider the unaudited financial results", A.RESULTS_DATE),
     ("Board Meeting Intimation", "to consider a fund raise by QIP", None),  # not results
     ("Corporate Action-Record Date", "record date for interim dividend", A.DIVIDEND),
     ("Bonus", "bonus issue 1:1", A.SPLIT_BONUS),
     ("Disclosure of reason for encumbrance", "pledge created", A.PLEDGE),
     ("Insider Trading / SAST", "promoter bought shares", A.INSIDER_OR_PROMOTER),
     ("General Updates", "monthly sales", None)],
)  # fmt: skip
def test_subject_rules(subject, text, expected):
    assert subject_type(subject, text) is expected


def test_event_dates():
    assert extract_event_date(
        "will meet on October 16, 2026 to consider", date(2026, 10, 5)
    ) == date(2026, 10, 16)
    assert extract_event_date("meeting on 16th Oct 2026", date(2026, 10, 5)) == date(2026, 10, 16)
    assert extract_event_date("on 16-Oct-2026", date(2026, 10, 5)) == date(2026, 10, 16)
    assert extract_event_date("on 16/10/2026", date(2026, 10, 5)) == date(2026, 10, 16)
    text = "results for the quarter ended September 30, 2026; meeting on November 2, 2026"
    assert extract_event_date(text, date(2026, 10, 5)) == date(2026, 11, 2)  # past dates skipped
    assert extract_event_date("no date here", date(2026, 10, 5)) is None


async def test_abstained_questions_stay_unknown():
    clf, _ = classifier(Scripted(LITIGATION, fail=True))  # Laya down, no Jev
    event = await clf.classify(announcement("Some update"))
    assert event.announcement_type is None and event.direction is None and event.materiality is None
    assert event.relevant is True  # unknown relevance stays visible to the gates
    assert event.extra["abstained"] == sorted(QUESTIONS) and event.model == "none"


def test_acceptance_no_portfolio_data_can_reach_a_decision_model_state():
    hints = typing.get_type_hints(announcement_state)
    assert hints["announcement"] is AnnouncementReceived  # the only input
    assert list(inspect.signature(announcement_state).parameters) == ["announcement", "max_tokens"]
    state = announcement_state(announcement("x" * 10_000, subject="General Updates"), 512)
    assert set(state) == {"instrument", "post", "subject"} and len(state["post"]) <= (512 - 96) * 4
    forbidden = {"position", "quantity", "pnl", "cash", "equity", "book"}
    assert not forbidden & {f.lower() for f in AnnouncementReceived.model_fields}


async def test_the_pipeline_classifies_what_it_ingests_and_survives_failures(tmp_path):
    clock = ReplayClock(T0)
    with EventStore(tmp_path / "rq.db") as store:
        sink = StoreSink(store, clock, "test")
        clf, _ = classifier(Scripted(LITIGATION), sink=sink)

        class Ingest:
            interval_s = 300.0

            async def poll(self, *, force: bool = False) -> PollStats:
                return PollStats(ok=True, new=[announcement("one", aid="a1"),
                                               announcement("two", aid="a2")])  # fmt: skip

        pipeline = AnnouncementPipeline(ingestor=Ingest(), classifier=clf)
        events = await pipeline.poll(force=True)
        assert [e.event_id for e in events] == ["a1", "a2"] and pipeline.interval_s == 300.0
        rows = store.query("SELECT event_id, announcement_type, direction FROM typed_events")
        assert sorted(r["event_id"] for r in rows) == ["a1", "a2"]
        assert rows[0]["announcement_type"] == "litigation_or_regulatory"

        class Exploding:
            async def classify(self, a: AnnouncementReceived) -> None:
                raise RuntimeError("bug")

        broken = AnnouncementPipeline(ingestor=Ingest(), classifier=Exploding())  # type: ignore[arg-type]
        assert await broken.classify_all([announcement("x", aid="a3")]) == []  # logged, not raised
        store_only = AnnouncementPipeline(ingestor=Ingest(), classifier=None,
                                          backlog=[announcement("old", aid="a0")])  # fmt: skip
        assert await store_only.poll() == []  # no decision model: stored, not classified
        later = AnnouncementPipeline(ingestor=Ingest(), classifier=clf,
                                     backlog=[announcement("old", aid="a0")])  # fmt: skip
        assert [e.event_id for e in await later.poll()] == ["a0", "a1", "a2"]  # backlog first

    left = unclassified([announcement("a", aid="a1"), announcement("b", aid="a9")], ["a1"])
    assert [a.announcement_id for a in left] == ["a9"]


def test_the_cascade_is_built_from_what_is_available(settings, monkeypatch):
    from pydantic import SecretStr

    import src.decision_models.setup as setup
    from src.decision_models.adapters import LayaLocal, SystemOneHTTP

    sink = RecordingSink(ReplayClock(T0))
    monkeypatch.setattr(setup, "laya_available", lambda: False)
    assert setup.build_cascade(settings, sink=sink) is None  # nothing: store, don't classify
    jev_only = setup.build_cascade(
        settings.model_copy(update={"typesafe_api_key": SecretStr("ts-key")}), sink=sink
    )
    assert (
        jev_only is not None and isinstance(jev_only._jev, SystemOneHTTP) and jev_only._laya is None
    )
    monkeypatch.setattr(setup, "laya_available", lambda: True)
    both = setup.build_cascade(
        settings.model_copy(update={"decision_laya_enabled": True}), sink=sink
    )
    assert both is not None and isinstance(both._laya, LayaLocal)
    assert both._laya.checkpoint == "multilingual" and both._laya._agent is None  # not loaded yet
    assert both.config.escalate_band == (0.35, 0.65) and both.config.shadow_pct == 0.2

"""Plan M8.5: replaying a recorded day - deterministic, golden, and fed only from recordings."""

from __future__ import annotations

import os
from datetime import datetime, time
from pathlib import Path

import pytest

from src.decision_models.cache import CachedDecisionModel, cache_key
from src.decision_models.tasks.announcements import QUESTIONS, announcement_state
from src.domain.events import AnnouncementReceived
from src.engine.live import DM_CACHE
from src.engine.replay import canonical_events, replay_day
from src.llm.router import StoreResponseCache
from src.store.event_store import EventStore
from src.store.sink import StoreSink
from src.utils.market_time import IST
from tests.test_engine_replay import DAY, INFY, TCS, record_tape

GOLDEN = Path(__file__).parent / "golden" / "replay_day_fixture.jsonl"


async def replayed(settings, tmp_path: Path, name: str, source_db: Path | None = None) -> list[str]:
    tape = tmp_path / "tape"
    if not tape.exists():
        record_tape(tape)
    db = await replay_day(settings, DAY, out_dir=tmp_path / name, tape_dir=tape,
                          universe=[INFY, TCS], source_db=source_db)  # fmt: skip
    with EventStore(db) as store:
        return canonical_events(store)


async def test_golden_a_fixture_tape_reproduces_identical_events(settings, tmp_path):
    first = await replayed(settings, tmp_path, "one")
    second = await replayed(settings, tmp_path, "two")
    assert first == second and len(first) > 100
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text("\n".join(first) + "\n", encoding="utf-8", newline="\n")
    golden = GOLDEN.read_text(encoding="utf-8").splitlines()
    assert first == golden, "the replay changed: inspect, then regenerate with UPDATE_GOLDEN=1"


async def test_the_replay_trades_writes_a_report_and_uses_no_network(settings, tmp_path):
    lines = await replayed(settings, tmp_path, "run")
    joined = "\n".join(lines)
    assert '"type": "TradeClosed"' in joined and '"exit_reason": "target"' in joined
    assert (tmp_path / "run" / "2026-10-05.md").exists()
    assert '"abstain_reason": "llm_role_veto_disabled"' in joined  # C: no LLM in tests
    assert '"type": "LLMCall"' not in joined


async def test_recorded_announcements_and_cached_answers_are_replayed(settings, tmp_path):
    published = datetime.combine(DAY, time(9, 40), IST)
    news = AnnouncementReceived(
        announcement_id="a1", instrument_key=INFY.key, company="Infosys Limited",
        published_at=published, received_at=published, subject="General Updates",
        title="Infosys Limited has informed the Exchange about a large order win", source="nse_rss",
    )  # fmt: skip
    source_db = tmp_path / "live.db"
    with EventStore(source_db) as live:
        clock_sink = StoreSink(
            live,
            __import__("src.domain.clock", fromlist=["ReplayClock"]).ReplayClock(published),
            "t",
        )
        clock_sink.emit(news)
        cache = StoreResponseCache(live, DM_CACHE)
        state = announcement_state(news, 1024)
        answers = {
            "relevant": {"type": "noul", "value": True, "probabilities": {"false": 0.05, "true": 0.95},
                         "confidence": 0.95, "model": "laya:multilingual", "calibrated": False},
            "event_type": {"type": "choice", "value": "order_win", "probabilities": {"order_win": 0.9},
                           "confidence": 0.9, "model": "laya:multilingual", "calibrated": False},
            "direction": {"type": "choice", "value": "positive", "probabilities": {"positive": 0.9},
                          "confidence": 0.9, "model": "laya:multilingual", "calibrated": False},
            "materiality": {"type": "score", "value": 1.0, "probabilities": {"moderate": 0.9},
                            "confidence": 0.9, "model": "laya:multilingual", "calibrated": False},
        }  # fmt: skip
        import json

        cache.put(cache_key("laya", settings.decision_laya_checkpoint, state, QUESTIONS),
                  json.dumps(answers), published)  # fmt: skip

    lines = await replayed(settings, tmp_path, "with_source", source_db=source_db)
    typed = [line for line in lines if '"type": "TypedEvent"' in line]
    assert len(typed) == 1 and '"announcement_type": "order_win"' in typed[0]
    assert '"direction": "positive"' in typed[0]
    with EventStore(source_db) as live:  # the replay never wrote to the source store
        assert [e.type for e in live.read()] == ["AnnouncementReceived"]


async def test_a_cache_miss_in_replay_never_calls_a_model():
    class Model:
        name, checkpoint, context_tokens = "laya", "multilingual", 1024

        async def decide(self, *a):
            raise AssertionError("called the model")

    class Empty:
        def get(self, key):
            return None

        def put(self, key, value, ts):
            raise AssertionError("wrote the cache")

    from src.decision_models.base import DecisionModelError

    with pytest.raises(DecisionModelError, match="replay cache"):
        await CachedDecisionModel(Model(), Empty(), replay_only=True).decide({"a": "b"}, QUESTIONS)

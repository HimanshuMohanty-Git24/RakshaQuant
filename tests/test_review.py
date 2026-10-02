"""Plan M8.7: the nightly review - grounded lessons per closed trade, never injected."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.domain.events import TradeReview
from src.engine.live import make_reporter
from src.evaluation.experiment import load_experiment
from src.evaluation.review import review_day, review_input
from src.llm.prompts.templates import ReviewOutput
from src.llm.registry import ModelSpec, RoleConfig
from src.llm.router import LLMResult
from tests.test_books import run as run_day
from tests.test_books import three_books
from tests.test_engine_replay import DAY

SRC = Path(__file__).resolve().parents[1] / "src"


class Router:
    def __init__(self, *, enabled: bool = True, fail_first: bool = False) -> None:
        chain = (ModelSpec.parse("anthropic:claude-sonnet-5-5"),) if enabled else ()
        self.roles = {"review": RoleConfig("review", chain)}
        self.calls: list[dict[str, Any]] = []
        self.fail_first = fail_first

    async def complete(self, role: str, messages: Any, schema: type, **kw: Any) -> LLMResult[Any]:
        self.calls.append(kw)
        assert role == "review" and "<data" in messages[-1].content
        if self.fail_first and len(self.calls) == 1:
            return LLMResult(role, "timeout")
        out = ReviewOutput(summary="The trade hit its target.", what_worked=["trend"],
                           what_failed=[], lessons=[
                               {"claim": "exits at target worked", "evidence_ref": "trade.exit_reason"},
                               {"claim": "made-up claim", "evidence_ref": "portfolio.cash"},
                           ], schema_version=1)  # fmt: skip
        return LLMResult(role, "ok", out, model="anthropic:claude-sonnet-5-5")


async def test_each_closed_trade_is_reviewed_once_with_grounded_lessons_only(tmp_path):
    with three_books(tmp_path) as (engine, clock):
        await run_day(engine, clock)
        closed = engine.store.query("SELECT trade_id FROM trades")
        router = Router(fail_first=True)
        first = await review_day(engine.store, router, DAY, clock=clock, sink=engine.sink)  # type: ignore[arg-type]
        assert len(first) == len(closed) - 1  # one call timed out: retried next time
        again = await review_day(engine.store, router, DAY, clock=clock, sink=engine.sink)  # type: ignore[arg-type]
        assert len(again) == 1  # only the one that failed; never twice
        reviews = [e.payload for e in engine.store.read(types=["TradeReview"])]
        assert len(reviews) == len(closed) and len({r.trade_id for r in reviews}) == len(closed)
        review = reviews[0]
        assert isinstance(review, TradeReview) and review.resolved_at == clock.now()
        assert [lesson.evidence_ref for lesson in review.lessons] == ["trade.exit_reason"]
        assert review.dropped_lessons == 1 and review.prompt_version.startswith("review_v1@")
        assert {c["book_id"] for c in router.calls} == {"A", "B", "C"}


async def test_the_review_input_is_the_trades_own_lineage(tmp_path):
    with three_books(tmp_path) as (engine, clock):
        await run_day(engine, clock)
        trade = next(
            e.payload for e in engine.store.read(types=["TradeClosed"]) if e.book_id == "B"
        )
        data = review_input(trade, list(engine.store.read(decision_id=trade.decision_id)))
        assert set(data) >= {"trade", "signal", "risk"} and data["trade"]["book"] == "B"
        assert data["risk"]["outcome"] in ("APPROVED", "RESIZED")


async def test_an_unconfigured_review_role_makes_no_call(tmp_path):
    with three_books(tmp_path) as (engine, clock):
        router = Router(enabled=False)
        assert await review_day(engine.store, router, DAY, clock=clock, sink=engine.sink) == []  # type: ignore[arg-type]
        assert router.calls == []


def test_reviews_are_never_fed_back_into_a_book():
    """Audit F-20: no decision, risk, strategy, order or engine code reads TradeReview."""
    forbidden = ("decision", "decision_models", "risk", "strategies", "oms", "brokers",
                 "features", "marketdata", "engine")  # fmt: skip
    offenders = []
    for package in forbidden:
        for path in (SRC / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "TradeReview" in text or "evaluation.review" in text:
                offenders.append(path.relative_to(SRC).as_posix())
    assert offenders == ["engine/live.py"]  # only to *run* the review after the report
    live = (SRC / "engine" / "live.py").read_text(encoding="utf-8")
    assert "TradeReview" not in live  # it never reads the reviews back


async def test_the_report_step_runs_the_review_and_survives_its_failure(settings, tmp_path):
    for name, router in (("ok", Router()), ("broken", Broken())):
        with three_books(tmp_path / name) as (engine, clock):
            engine.reporter = make_reporter(engine, load_experiment(), settings,
                                            tmp_path / name / "reports", notify=False,
                                            router=router)  # type: ignore[arg-type]  # fmt: skip
            assert await run_day(engine, clock) == 0
            assert (tmp_path / name / "reports" / f"{DAY}.md").exists()
            reviews = list(engine.store.read(types=["TradeReview"]))
            alerts = [e.payload.key for e in engine.store.read(types=["Alert"])]
            if name == "ok":
                assert len(reviews) == len(engine.store.query("SELECT trade_id FROM trades")) > 0
                assert "nightly_review_failed" not in alerts
            else:
                assert reviews == [] and "nightly_review_failed" in alerts


class Broken(Router):
    async def complete(self, role: str, messages: Any, schema: type, **kw: Any) -> LLMResult[Any]:
        raise RuntimeError("store locked")

"""
Operator controls (plan M9.3; audit §L.4, §N.1 #1): halt, resume and flatten the books' global
kill switches, each recorded as a ``ControlCommand`` event whether or not it changed anything.

With a run in this process the engine's own :class:`KillSwitchRegistry` objects are used, so the
RiskGate sees the change on its next check. Without one, the switches' persisted state in the
store is changed directly (the web server holds the environment's process lock, so no other
engine can be running on it); the next session starts with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from src.domain.events import ControlCommand
from src.domain.sink import EventSink
from src.domain.types import KillScope, KillSwitchState
from src.engine.runner import acknowledging
from src.risk.kill_switch import KillSwitchRegistry
from src.risk.state import DailyRiskTracker
from src.store.kv import KVStateStore
from src.store.sink import StoreSink
from src.web.models import ControlResult

if TYPE_CHECKING:
    from src.web.run_manager import RunManager

ACTOR = "web"
Outcome = Literal["applied", "no_change", "refused"]


class ControlRefusedError(Exception):
    """A control the server will not carry out; ``status`` is the HTTP status to answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Controls:
    def __init__(self, manager: RunManager) -> None:
        self.manager = manager

    def _targets(self, book: str | None) -> list[str]:
        books = self.manager.queries().books
        if book is None:
            return books
        if book not in books:
            raise ControlRefusedError(404, "unknown book")
        return [book]

    def _switches(self) -> tuple[dict[str, KillSwitchRegistry], EventSink]:
        engine = self.manager.engine
        if engine is not None:
            return {b: book.switches for b, book in engine.books.items()}, engine.sink
        q = self.manager.queries()
        sink = StoreSink(q.store, q.clock, ACTOR)
        registries = {
            b: KillSwitchRegistry(
                book_id=b,
                limits=q.limits,
                clock=q.clock,
                sink=sink,
                state_store=KVStateStore(q.store, b, "kill_switches"),
                halt_file=self.manager.settings.halt_file,
                on_resume=acknowledging(
                    DailyRiskTracker(
                        book_id=b,
                        limits=q.limits,
                        clock=q.clock,
                        sink=sink,
                        state_store=KVStateStore(q.store, b, "daily_risk"),
                    )
                ),
            )  # fmt: skip
            for b in q.books
        }
        return registries, sink

    def record(self, sink: EventSink, action: str, outcome: Outcome, books: list[str],
               detail: str) -> ControlResult:  # fmt: skip
        sink.emit(ControlCommand(action=action, actor=ACTOR, outcome=outcome, books=tuple(books),
                                 detail=detail), source=ACTOR)  # fmt: skip
        return ControlResult(action=action, outcome=outcome, books=books, detail=detail)

    def _when(self) -> str:
        if self.manager.engine is not None:
            return "now"
        return "when the next session starts"

    def halt(self, *, book: str | None, reason: str) -> ControlResult:
        """Block new entries (HALT_NEW). Allowed even in read-only mode."""
        targets = self._targets(book)
        registries, sink = self._switches()
        changed = [b for b in targets
                   if registries[b].trip(KillScope.GLOBAL, KillSwitchState.HALT_NEW,
                                         reason=f"operator: {reason}", actor=ACTOR,
                                         code="API_HALT")]  # fmt: skip
        outcome: Outcome = "applied" if changed else "no_change"
        return self.record(sink, "halt", outcome, changed or targets,
                           f"new entries blocked {self._when()}: {reason}")  # fmt: skip

    def flatten(self, *, book: str | None, reason: str) -> ControlResult:
        """Exit every position through the OMS (FLATTEN); the Flattener acts on its next tick."""
        targets = self._targets(book)
        registries, sink = self._switches()
        changed = [b for b in targets
                   if registries[b].trip(KillScope.GLOBAL, KillSwitchState.FLATTEN,
                                         reason=f"operator: {reason}", actor=ACTOR,
                                         code="API_FLATTEN")]  # fmt: skip
        when = ("at the next risk tick (within 60 s)" if self.manager.engine is not None
                else "when the next session starts")  # fmt: skip
        outcome: Outcome = "applied" if changed else "no_change"
        return self.record(sink, "flatten", outcome, changed or targets,
                           f"positions flattened {when}: {reason}")  # fmt: skip

    def resume(self, *, book: str | None, reason: str, scope: KillScope,
               name: str | None) -> ControlResult:  # fmt: skip
        """Re-arm a switch (latching switches only ever re-arm by hand)."""
        if scope is KillScope.STRATEGY and not name:
            raise ControlRefusedError(422, "a strategy switch needs its name")
        switch = name if scope is KillScope.STRATEGY and name else scope.value
        targets = self._targets(book)
        registries, sink = self._switches()
        changed = []
        for b in targets:
            try:
                if registries[b].resume(scope, actor=ACTOR, reason=f"operator: {reason}",
                                        name=switch):  # fmt: skip
                    changed.append(b)
            except RuntimeError:  # the HALT file is still present
                self.record(sink, "resume", "refused", [b], "a HALT file is present")
                raise ControlRefusedError(409, "a HALT file is present: remove it first") from None
        outcome: Outcome = "applied" if changed else "no_change"
        return self.record(sink, "resume", outcome, changed or targets,
                           f"{scope.value}/{switch} re-armed: {reason}")  # fmt: skip

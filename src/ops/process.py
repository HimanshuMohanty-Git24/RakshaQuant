"""
Entry-point runner (plan M1.6): every ``scripts/*.py`` runs its ``main`` through
:func:`run_entry_point`, which gives all of them the same process contract:

* logging configured (JSON file under ``var/logs/`` + optional console), secrets redacted;
* exit codes: 0 normal (incl. Ctrl-C), 1 crash, 2 configuration error, 3 lock held;
* optionally the single-instance lock, and ``ProcessStarted`` / ``ProcessStopped(reason)``
  events in the environment's event store.

A configuration error never echoes input values (they may be secrets).
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import time
from collections.abc import Callable
from typing import NoReturn

from pydantic import ValidationError

from src.config.errors import ConfigError
from src.config.settings import Settings, get_settings
from src.domain.clock import WallClock
from src.domain.events import ProcessStarted, ProcessStopped, make_event
from src.ops.exit_codes import ExitCode
from src.ops.instance_lock import InstanceLockHeldError, single_instance
from src.ops.logging_config import configure_logging, redact
from src.ops.version import code_version

logger = logging.getLogger(__name__)

__all__ = ["ConfigError", "run", "run_entry_point"]


EntryMain = Callable[[], int | None]


def run_entry_point(
    name: str,
    main: EntryMain,
    *,
    lock: bool = False,
    record_events: bool = False,
    console_log_level: str | None = "WARNING",
) -> NoReturn:
    """Run ``main`` under the process contract and exit with its exit code."""
    sys.exit(run(name, main, lock=lock, record_events=record_events,
                 console_log_level=console_log_level))  # fmt: skip


def run(
    name: str,
    main: EntryMain,
    *,
    lock: bool = False,
    record_events: bool = False,
    console_log_level: str | None = "WARNING",
) -> int:
    """Like :func:`run_entry_point` but returns the exit code (for tests and embedding)."""
    try:
        settings = get_settings()
    except ValidationError as exc:
        _stderr(f"{name}: configuration error\n{_describe(exc)}")
        return ExitCode.CONFIG_ERROR

    logs = configure_logging(
        settings.logs_dir, level=settings.log_level, console_level=console_log_level
    )
    try:
        with contextlib.ExitStack() as stack:
            if lock:
                try:
                    stack.enter_context(single_instance(settings.state_dir))
                except InstanceLockHeldError as exc:
                    logger.error("%s refused to start: %s", name, exc)
                    _stderr(str(exc))
                    return ExitCode.LOCK_HELD
            return _supervise(name, main, settings, record_events)
    finally:
        logs.close()


def _supervise(name: str, main: EntryMain, settings: Settings, record_events: bool) -> int:
    clock = WallClock()
    started = time.monotonic()
    store = None
    if record_events:
        from src.store.event_store import EventStore  # only processes that record events

        store = EventStore(settings.db_path)
        store.append(
            make_event(
                ProcessStarted(
                    pid=os.getpid(),
                    entry_point=name,
                    argv=tuple(sys.argv[1:]),
                    environment=settings.environment,
                    version=code_version(),
                ),
                ts=clock.now(),
                source="process",
            )
        )
    logger.info("%s started (environment=%s, pid=%d)", name, settings.environment, os.getpid())

    code: int = ExitCode.CRASH
    reason = "crash"
    try:
        result = main()
        code = int(result or 0)
        reason = "normal" if code == 0 else f"exit_{code}"
    except KeyboardInterrupt:
        code, reason = ExitCode.OK, "interrupted"
    except ConfigError as exc:
        logger.error("%s configuration error: %s", name, exc)
        _stderr(f"{name}: configuration error: {redact(str(exc))}")
        code, reason = ExitCode.CONFIG_ERROR, "config_error"
    except SystemExit as exc:
        code = _system_exit_code(exc)
        reason = "normal" if code == 0 else f"exit_{code}"
    except Exception as exc:
        logger.exception("%s crashed", name)
        _stderr(
            f"{name} crashed: {type(exc).__name__}: {redact(str(exc))} (see {settings.logs_dir})"
        )
        code, reason = ExitCode.CRASH, "crash"
    finally:
        uptime = time.monotonic() - started
        logger.info("%s stopped: %s (exit %d, %.1fs)", name, reason, code, uptime)
        if store is not None:
            try:
                store.append(
                    make_event(
                        ProcessStopped(
                            pid=os.getpid(),
                            entry_point=name,
                            reason=reason,
                            exit_code=code,
                            uptime_s=uptime,
                        ),
                        ts=clock.now(),
                        source="process",
                    )
                )
            except Exception:  # never mask the real exit code
                logger.exception("could not record ProcessStopped")
            finally:
                store.close()
    return code


def _system_exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return ExitCode.OK
    if isinstance(exc.code, int):
        return exc.code
    _stderr(redact(str(exc.code)))
    return ExitCode.CRASH


def _describe(exc: ValidationError) -> str:
    """Field locations and messages only; never the offending input values."""
    lines = []
    for error in exc.errors(include_input=False, include_url=False):
        where = ".".join(str(part) for part in error["loc"]) or "settings"
        lines.append(f"  - {where}: {error['msg']}")
    return "\n".join(lines)


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)

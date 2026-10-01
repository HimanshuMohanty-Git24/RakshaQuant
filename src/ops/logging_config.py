"""
Logging (plan M1.6; audit §O.2).

* A JSON-lines file per IST day: ``<logs_dir>/rakshaquant-YYYYMMDD.log``.
* An optional human console handler (off while the Rich dashboard owns the terminal).
* Every record carries the lineage context (``cycle_id``, ``decision_id``, ``component``,
  ``symbol``, ``book_id``).
* Secrets are redacted from the *final* output of every handler, tracebacks included.

Logs are for humans; events (the event store) are the system of record.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Any

from src.ops.context import CONTEXT_FIELDS, current_context
from src.utils.market_time import IST

# (pattern, replacement). Order matters: specific key=value forms before bare tokens.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"bot\d+:[\w-]+"), "bot[REDACTED]"),
    (re.compile(r"(?i)\b(bearer\s+)[\w.~+/-]+=*"), r"\1[REDACTED]"),
    (
        re.compile(r"(?i)\b(access[_-]?token|api[_-]?key|token|secret|password)=\S+"),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(
            r"""(?i)(["']?(?:access[_-]?token|api[_-]?key|authorization|client[_-]?secret)"""
            r"""["']?\s*:\s*["']?)[^"'\s,}]+"""
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"://[^:@/\s]+:[^@/\s]+@"), "://[REDACTED]@"),
    # Wider than the plan's sk-[A-Za-z0-9]{8,}: Anthropic (sk-ant-...) and OpenRouter
    # (sk-or-v1-...) keys contain hyphens.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
    (re.compile(r"\bgsk_\S+"), "[REDACTED]"),
    (re.compile(r"\blsv2_\S+"), "[REDACTED]"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "[REDACTED]"),
)

_NOISY_LOGGERS = ("httpx", "httpx2", "httpcore", "urllib3", "yfinance", "peewee", "filelock")


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class ContextFilter(logging.Filter):
    """Copies the lineage contextvars onto each record (without overriding explicit extras)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name, value in current_context().items():
            if not hasattr(record, name):
                setattr(record, name, value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Redact each value *before* serialising: redacting the JSON text could swallow quotes.
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for name in CONTEXT_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                data[name] = redact(str(value))
        if record.exc_info:
            data["exc"] = redact(self.formatException(record.exc_info))
        elif record.exc_text:
            data["exc"] = redact(record.exc_text)
        return json.dumps(data, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class DailyFileHandler(logging.Handler):
    """Appends to ``rakshaquant-YYYYMMDD.log``, switching files when the IST date changes."""

    def __init__(self, logs_dir: Path, now: Callable[[], datetime] | None = None) -> None:
        super().__init__()
        self.logs_dir = logs_dir
        self._now = now or (lambda: datetime.now(UTC))
        self._day: date | None = None
        self._stream: IO[str] | None = None

    def path_for(self, day: date) -> Path:
        return self.logs_dir / f"rakshaquant-{day:%Y%m%d}.log"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            day = self._now().astimezone(IST).date()
            stream = self._stream
            if stream is None or day != self._day:
                stream = self._open(day)
            stream.write(line + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)

    def _open(self, day: date) -> IO[str]:
        if self._stream is not None:
            self._stream.close()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        stream = self.path_for(day).open("a", encoding="utf-8")
        self._stream, self._day = stream, day
        return stream

    def close(self) -> None:
        self.acquire()
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            self.release()
            super().close()


@dataclass
class LoggingHandle:
    """What :func:`configure_logging` changed, so :meth:`close` can undo it exactly."""

    handlers: list[logging.Handler]
    previous_root_level: int
    previous_levels: dict[str, int] = field(default_factory=dict)

    @property
    def file_handler(self) -> DailyFileHandler:
        for handler in self.handlers:
            if isinstance(handler, DailyFileHandler):
                return handler
        raise LookupError("no file handler installed")

    def close(self) -> None:
        root = logging.getLogger()
        for handler in self.handlers:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(self.previous_root_level)
        for name, level in self.previous_levels.items():
            logging.getLogger(name).setLevel(level)


def configure_logging(
    logs_dir: Path,
    *,
    level: str = "INFO",
    console_level: str | None = "WARNING",
    console_stream: IO[str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> LoggingHandle:
    """Install the JSON file handler (and optionally a console handler) on the root logger."""
    root = logging.getLogger()
    context = ContextFilter()
    file_handler = DailyFileHandler(logs_dir, now=now)
    file_handler.setFormatter(JsonFormatter())
    file_handler.setLevel(level)
    file_handler.addFilter(context)
    handlers: list[logging.Handler] = [file_handler]
    levels = [logging.getLevelName(level)]
    if console_level is not None:
        console = logging.StreamHandler(console_stream or sys.stderr)
        console.setFormatter(ConsoleFormatter())
        console.setLevel(console_level)
        console.addFilter(context)
        handlers.append(console)
        levels.append(logging.getLevelName(console_level))

    handle = LoggingHandle(handlers=handlers, previous_root_level=root.level)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(min(levels))
    for name in _NOISY_LOGGERS:
        noisy = logging.getLogger(name)
        handle.previous_levels[name] = noisy.level
        noisy.setLevel(max(logging.WARNING, noisy.level))
    return handle

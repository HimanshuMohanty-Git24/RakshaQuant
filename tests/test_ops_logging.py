"""Plan M1.6: structured logs with lineage context, daily files, and secret redaction."""

import asyncio
import io
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.ops.context import current_context, log_context
from src.ops.logging_config import DailyFileHandler, configure_logging, redact

log = logging.getLogger("rq.test")

SECRETS = {
    "telegram bot url": "https://api.telegram.org/bot123456789:AAH-xyz_SECRET/sendMessage",
    "token=": "GET /x?token=SECRETVALUE&a=1",
    "api_key=": "api_key=SECRETVALUE",
    "access-token=": "access-token=SECRETVALUE",
    "header dict": "{'access-token': 'SECRETVALUE', 'client-id': '1100'}",
    "json api key": '{"api_key": "SECRETVALUE"}',
    "bearer": "Authorization: Bearer SECRETVALUE.part2",
    "db url": "postgresql://user:SECRETVALUE@localhost:5432/db",
    "openai": "key sk-proj-SECRETVALUE0123",
    "anthropic": "key sk-ant-api03-SECRETVALUE-abc",
    "openrouter": "key sk-or-v1-SECRETVALUE",
    "groq": "gsk_SECRETVALUE123",
    "langsmith": "lsv2_pt_SECRETVALUE",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SECRETVALUEsig",
}


@pytest.mark.parametrize("text", SECRETS.values(), ids=SECRETS.keys())
def test_redaction_strips_every_secret_shape(text):
    out = redact(text)
    assert "SECRETVALUE" not in out and "SECRET" not in out.replace("REDACTED", "")
    assert "[REDACTED]" in out or "bot[REDACTED]" in out


@pytest.mark.parametrize(
    "text",
    [
        "Placed BUY 13 INFY @ 1512.40",
        "token count 1200 tokens",
        "https://www.nseindia.com/api/holiday-master?type=trading",
        "skip this",
    ],
)
def test_redaction_leaves_ordinary_text_alone(text):
    assert redact(text) == text


@pytest.fixture
def logs(tmp_path: Path):
    stream = io.StringIO()
    handle = configure_logging(tmp_path / "logs", level="DEBUG", console_level="INFO",
                               console_stream=stream)  # fmt: skip
    yield tmp_path / "logs", stream, handle
    handle.close()


def _records(logs_dir: Path) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for path in sorted(logs_dir.glob("rakshaquant-*.log")):
        lines += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return lines


def test_json_lines_carry_lineage_and_are_redacted(logs):
    logs_dir, console, handle = logs
    with log_context(cycle_id="cy-1", decision_id="d-1", component="oms", book_id="B"):
        log.info("submitting with api_key=%s", "SECRETVALUE")
    handle.file_handler.flush()
    (record,) = [r for r in _records(logs_dir) if r["logger"] == "rq.test"]
    assert record["msg"] == "submitting with api_key=[REDACTED]"
    assert (record["cycle_id"], record["decision_id"], record["component"], record["book_id"]) == (
        "cy-1",
        "d-1",
        "oms",
        "B",
    )
    assert record["level"] == "INFO" and str(record["ts"]).endswith("+00:00")
    assert "SECRETVALUE" not in console.getvalue() and "api_key=[REDACTED]" in console.getvalue()


def test_tracebacks_are_redacted(logs):
    logs_dir, console, _ = logs
    try:
        raise RuntimeError("POST https://api.telegram.org/bot123:AAAsecretTOKEN/sendMessage")
    except RuntimeError:
        log.exception("telegram failed")
    (record,) = [r for r in _records(logs_dir) if r["logger"] == "rq.test"]
    assert "Traceback" in str(record["exc"])
    assert "AAAsecretTOKEN" not in json.dumps(record)
    assert "AAAsecretTOKEN" not in console.getvalue()


async def test_context_follows_work_into_threads_and_tasks(logs):
    logs_dir, _, _ = logs
    with log_context(cycle_id="cy-9", decision_id="d-9"):
        await asyncio.to_thread(log.info, "from a worker thread")
        await asyncio.create_task(asyncio.to_thread(log.info, "from a task's thread"))
    log.info("outside")
    by_msg = {r["msg"]: r for r in _records(logs_dir) if r["logger"] == "rq.test"}
    assert by_msg["from a worker thread"]["cycle_id"] == "cy-9"
    assert by_msg["from a task's thread"]["decision_id"] == "d-9"
    assert "cycle_id" not in by_msg["outside"]


def test_log_context_nests_and_restores():
    with log_context(cycle_id="outer", component="engine"):
        with log_context(cycle_id="inner"):
            assert current_context()["cycle_id"] == "inner"
            assert current_context()["component"] == "engine"
        assert current_context()["cycle_id"] == "outer"
    assert current_context()["cycle_id"] is None
    with pytest.raises(TypeError, match="unknown"), log_context(trade="x"):
        pass


def test_daily_file_switches_at_ist_midnight(tmp_path):
    now = [datetime(2026, 10, 5, 18, 29, tzinfo=UTC)]  # 23:59 IST
    handler = DailyFileHandler(tmp_path, now=lambda: now[0])
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(logging.makeLogRecord({"msg": "before midnight"}))
    now[0] += timedelta(minutes=2)  # 00:01 IST on the 6th
    handler.emit(logging.makeLogRecord({"msg": "after midnight"}))
    handler.close()
    assert (tmp_path / "rakshaquant-20261005.log").read_text().strip() == "before midnight"
    assert (tmp_path / "rakshaquant-20261006.log").read_text().strip() == "after midnight"


def test_configure_and_close_restore_the_root_logger(tmp_path):
    root = logging.getLogger()
    before_handlers, before_level = list(root.handlers), root.level
    handle = configure_logging(tmp_path, console_level=None)
    assert len(root.handlers) == len(before_handlers) + 1
    assert logging.getLogger("yfinance").level >= logging.WARNING
    handle.close()
    assert root.handlers == before_handlers and root.level == before_level

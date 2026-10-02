"""Plan M9.5: the API contract (OpenAPI → generated TypeScript) never drifts from the code."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import get_args

from src.web.models import StreamTopic
from src.web.stream import TOPICS

ROOT = Path(__file__).resolve().parents[1]


def _export_module():
    spec = importlib.util.spec_from_file_location("export_openapi",
                                                  ROOT / "scripts" / "export_openapi.py")  # fmt: skip
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_openapi_document_is_current():
    committed = (ROOT / "frontend" / "openapi.json").read_text(encoding="utf-8")
    assert _export_module().document() == committed, (
        "run `uv run --extra web python scripts/export_openapi.py`, then `npm run gen:api` in "
        "frontend/ (CI regenerates the TypeScript and fails on any difference)"
    )


def test_the_contract_covers_the_api_and_the_stream():
    doc = json.loads((ROOT / "frontend" / "openapi.json").read_text(encoding="utf-8"))
    for path in ("/api/summary", "/api/decisions/{decision_id}", "/api/risk/resume",
                 "/api/session/stop", "/api/market/{symbol}/bars"):  # fmt: skip
        assert path in doc["paths"]
    schemas = doc["components"]["schemas"]
    assert {"StreamEnvelope", "StreamSubscribe", "ErrorBody", "Summary"} <= set(schemas)
    assert "HTTPValidationError" not in schemas  # our 422 body is ErrorBody
    assert set(get_args(StreamTopic)) == TOPICS | {"events"}
    generated = (ROOT / "frontend" / "src" / "api" / "types.gen.ts").read_text(encoding="utf-8")
    assert "StreamEnvelope: {" in generated and '"/api/summary": {' in generated

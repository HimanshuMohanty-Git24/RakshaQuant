"""
Smoke tests for the web console: the run-control guards and the FastAPI REST + WebSocket
surface. No real trading run is started here.
"""

import pytest

from src.web.run_manager import RunControlError, RunManager
from src.web.server import create_app
from src.web.stream import _parse
from tests.web_helpers import PROTOCOLS, SECURITY, WS_URL, authed


async def test_run_manager_readonly_guard(monkeypatch):
    monkeypatch.setenv("RAKSHAQUANT_WEB_READONLY", "1")
    with pytest.raises(RunControlError):
        await RunManager().start(demo=True)


async def test_run_manager_readonly_blocks_stop(monkeypatch):
    # Read-only disables run-control ENTIRELY — stopping is a control action too.
    monkeypatch.setenv("RAKSHAQUANT_WEB_READONLY", "1")
    with pytest.raises(RunControlError):
        await RunManager().stop()


# ── FastAPI surface ─────────────────────────────────────────────────────────────


def test_rest_endpoints():
    client = authed(create_app(security=SECURITY))
    assert client.get("/api/health").json() == {"status": "ok"}  # liveness only
    assert client.get("/api/state").status_code == 404  # the legacy console snapshot is gone

    cfg = client.get("/api/config").json()
    assert cfg["mode"] == "paper" and "allowLiveOrders" not in cfg  # v2: no broker path


def test_websocket_subscribe_contract():
    client = authed(create_app(security=SECURITY))
    with client.websocket_connect(WS_URL, subprotocols=PROTOCOLS) as ws:
        assert ws.accepted_subprotocol == "rq.v1"
        ws.send_json({"subscribe": ["summary", "system"], "since_seq": 0})
        msg = ws.receive_json()
        assert msg["v"] == 1 and msg["type"] == "subscribed"
        assert msg["data"]["topics"] == ["summary", "system"]
    assert _parse('{"subscribe": ["console"]}') is None  # the legacy console's topic is gone


def test_run_stop_readonly_returns_403(monkeypatch):
    # Read-only refuses run control with 403, not a 500.
    monkeypatch.setenv("RAKSHAQUANT_WEB_READONLY", "1")
    client = authed(create_app(security=SECURITY))
    res = client.post("/api/session/stop")
    assert res.status_code == 403
    assert "error" in res.json()


def test_the_spa_is_served_for_deep_links_but_never_outside_its_folder(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    client = authed(create_app(security=SECURITY, frontend_dist=dist))
    assert "id=root" in client.get("/decisions/abc123").text  # the client routes it
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/favicon.svg").text == "<svg/>"
    assert "nope" not in client.get("/..%2Fsecret.txt").text
    assert client.get("/api/does-not-exist").status_code == 404
    assert client.get("/api/health").json() == {"status": "ok"}

"""Plan M9.1 acceptance: the web control plane is authenticated and same-origin only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.config.errors import ConfigError
from src.web.run_manager import RunManager
from src.web.security import WebSecurity, check_bind
from src.web.server import create_app
from tests.web_helpers import (
    AUTH,
    BASE,
    ORIGIN,
    PROTOCOLS,
    SECURITY,
    TOKEN,
    WS_URL,
    anonymous,
    authed,
)


@pytest.fixture
def app():
    return create_app(security=SECURITY)


def test_a_cross_origin_post_is_forbidden_even_with_the_token(app):
    client = authed(app)
    res = client.post("/api/session/stop", headers={"Origin": "https://evil.example"})
    assert res.status_code == 403 and res.json() == {"error": "forbidden"}
    res = client.post("/api/session/stop", headers={"Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 403
    assert client.post("/api/session/stop", headers={"Origin": ORIGIN}).status_code == 200


def test_a_missing_or_wrong_token_is_unauthorized(app):
    assert anonymous(app).get("/api/summary").status_code == 401
    assert anonymous(app).post("/api/session/stop", headers={"Origin": ORIGIN}).status_code == 401
    wrong = anonymous(app).get("/api/summary", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401 and wrong.headers["www-authenticate"] == "Bearer"
    basic = anonymous(app).get("/api/summary", headers={"Authorization": f"Basic {TOKEN}"})
    assert basic.status_code == 401
    assert anonymous(app).get("/api/health").json() == {"status": "ok"}  # liveness is public


def test_a_foreign_host_header_is_rejected(app):
    """DNS rebinding: a page on attacker.example resolving to 127.0.0.1."""
    res = TestClient(app, base_url="http://attacker.example:8000", headers=AUTH).get("/api/summary")
    assert res.status_code == 400
    assert (
        TestClient(app, base_url="http://localhost:8000", headers=AUTH)
        .get("/api/summary")
        .status_code
        == 200
    )


def _ws_close_code(app, **kwargs) -> int:
    with pytest.raises(WebSocketDisconnect) as refused:
        with TestClient(app, base_url=BASE).websocket_connect(WS_URL, **kwargs) as ws:
            ws.receive_json()
    return refused.value.code


def test_the_websocket_needs_our_origin_and_the_token(app):
    bad_origin = {"subprotocols": PROTOCOLS, "headers": {"Origin": "https://evil.example"}}
    assert _ws_close_code(app, **bad_origin) == 1008
    assert _ws_close_code(app, headers={"Origin": ORIGIN}) == 1008  # no token
    assert _ws_close_code(app, subprotocols=["rq.v1", "rq.token.wrong"]) == 1008
    with TestClient(app, base_url=BASE).websocket_connect(
        WS_URL, subprotocols=PROTOCOLS, headers={"Origin": ORIGIN}
    ) as ws:
        assert ws.accepted_subprotocol == "rq.v1"  # never the token
        ws.send_json({"subscribe": ["system"]})
        assert ws.receive_json()["type"] == "subscribed"


def test_websocket_connections_are_capped():
    app = create_app(security=WebSecurity.for_launch("127.0.0.1", 8000, token=TOKEN,
                                                     max_websockets=1))  # fmt: skip
    client = TestClient(app, base_url=BASE)
    with client.websocket_connect(WS_URL, subprotocols=PROTOCOLS) as first:
        first.send_json({"subscribe": ["system"]})
        first.receive_json()
        assert _ws_close_code(app, subprotocols=PROTOCOLS) == 1013
    with client.websocket_connect(WS_URL, subprotocols=PROTOCOLS) as again:  # slot released
        again.send_json({"subscribe": ["system"]})
        again.receive_json()


def test_bodies_are_strict_and_errors_do_not_echo_input(app):
    client = authed(app)
    res = client.post("/api/session/start", json={"demo": "false"})
    assert res.status_code == 422 and res.json() == {"error": "invalid request",
                                                     "fields": ["body.demo"]}  # fmt: skip
    res = client.post("/api/session/start", json={"demo": "secret-looking-value"})
    assert res.status_code == 422 and "secret-looking-value" not in res.text
    assert client.post("/api/session/start", json={"confirmLive": True}).status_code == 422


def test_an_internal_error_is_generic():
    class Exploding(RunManager):
        def live_view(self):
            raise RuntimeError("C:/Users/someone/.env could not be parsed")

    client = TestClient(create_app(manager=Exploding(), security=SECURITY), base_url=BASE,
                        headers=AUTH, raise_server_exceptions=False)  # fmt: skip
    res = client.get("/api/summary")
    assert res.status_code == 500 and res.json() == {"error": "internal error"}


def test_binding_beyond_loopback_needs_allow_remote():
    for host in ("127.0.0.1", "localhost", "::1"):
        check_bind(host, allow_remote=False)
    with pytest.raises(ConfigError, match="--allow-remote"):
        check_bind("0.0.0.0", allow_remote=False)  # noqa: S104
    with pytest.raises(ConfigError):
        WebSecurity.for_launch("192.168.1.20", 8000)
    remote = WebSecurity.for_launch("192.168.1.20", 8000, allow_remote=True)
    assert "192.168.1.20" in remote.allowed_hosts
    assert "http://192.168.1.20:8000" in remote.allowed_origins


def test_each_launch_has_its_own_token_in_the_url_fragment():
    one, two = (WebSecurity.for_launch("127.0.0.1", 8000) for _ in range(2))
    assert one.token != two.token and len(one.token) >= 43
    assert one.url("127.0.0.1", 8000) == f"http://127.0.0.1:8000/#token={one.token}"
    assert one.allowed_origins == {"http://127.0.0.1:8000", "http://localhost:8000"}
    dev = WebSecurity.for_launch("127.0.0.1", 8000, dev=True)
    assert "http://localhost:5173" in dev.allowed_origins

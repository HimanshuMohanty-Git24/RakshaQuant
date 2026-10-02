"""
The web control plane's security (plan M9.1; audit §N.1 #1, §P.1).

* A **per-launch token** (``secrets.token_urlsafe(32)``), printed once as a URL with a
  ``#token=`` fragment (fragments never reach a server log), required as
  ``Authorization: Bearer <token>`` on ``/api/*`` and as the WebSocket subprotocol
  ``rq.token.<token>`` (browsers cannot set headers on a WebSocket).
* ``TrustedHostMiddleware`` for loopback names only (DNS rebinding → 400).
* An **Origin allowlist** on every state-changing request and on the WebSocket handshake
  (CSRF and cross-site WebSocket hijacking → 403 / close 1008).
* A non-loopback bind is refused unless ``--allow-remote`` is given.
* A cap on concurrent WebSocket connections; generic error messages (no exception text).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, WebSocket

from src.config.errors import ConfigError

LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
_LOOPBACK_BINDS = frozenset({*LOOPBACK_HOSTS, "::1"})
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")  # the Vite dev server
WS_PROTOCOL = "rq.v1"
WS_TOKEN_PREFIX = "rq.token."
WS_POLICY_VIOLATION = 1008
WS_TRY_AGAIN_LATER = 1013
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def new_token() -> str:
    return secrets.token_urlsafe(32)


def check_bind(host: str, *, allow_remote: bool) -> None:
    """Refuse to listen beyond this machine unless explicitly allowed."""
    if host not in _LOOPBACK_BINDS and not allow_remote:
        raise ConfigError(f"refusing to bind the web console to {host!r}: it is reachable from "
                          "other machines. Pass --allow-remote if that is intended.")  # fmt: skip


@dataclass(frozen=True)
class WebSecurity:
    token: str
    allowed_hosts: tuple[str, ...] = LOOPBACK_HOSTS
    allowed_origins: frozenset[str] = field(default_factory=frozenset)
    max_websockets: int = 8

    @classmethod
    def for_launch(
        cls,
        host: str,
        port: int,
        *,
        token: str | None = None,
        dev: bool = False,
        allow_remote: bool = False,
        max_websockets: int = 8,
    ) -> WebSecurity:
        check_bind(host, allow_remote=allow_remote)
        hosts = list(LOOPBACK_HOSTS)
        if host not in _LOOPBACK_BINDS:
            hosts.append(host)
        origins = {f"http://{h}:{port}" for h in hosts}
        if dev:
            origins.update(DEV_ORIGINS)
        return cls(token=token or new_token(), allowed_hosts=tuple(hosts),
                   allowed_origins=frozenset(origins), max_websockets=max_websockets)  # fmt: skip

    def url(self, host: str, port: int) -> str:
        """The one-time launch URL; the token travels in the fragment."""
        shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host  # noqa: S104
        return f"http://{shown}:{port}/#token={self.token}"

    def token_ok(self, presented: str | None) -> bool:
        return presented is not None and secrets.compare_digest(
            presented.encode(), self.token.encode()
        )

    def origin_ok(self, origin: str | None) -> bool:
        """A missing Origin is a non-browser client (it still needs the token)."""
        return origin is None or origin in self.allowed_origins


def _security(app_state: object) -> WebSecurity:
    sec = getattr(app_state, "security", None)
    if not isinstance(sec, WebSecurity):  # pragma: no cover - create_app always sets it
        raise HTTPException(status_code=500, detail="internal error")
    return sec


async def require_same_origin(request: Request) -> None:
    """State-changing requests must come from the console's own origin (or no browser)."""
    if request.method in _SAFE_METHODS:
        return
    sec = _security(request.app.state)
    if request.headers.get("sec-fetch-site") == "cross-site" or not sec.origin_ok(
        request.headers.get("origin")
    ):
        raise HTTPException(status_code=403, detail="forbidden")


async def require_token(request: Request) -> None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not _security(request.app.state).token_ok(value.strip()):
        raise HTTPException(status_code=401, detail="unauthorized",
                            headers={"WWW-Authenticate": "Bearer"})  # fmt: skip


def websocket_refusal(websocket: WebSocket, open_connections: int) -> int | None:
    """The close code to refuse a WebSocket handshake with, or None to accept it."""
    sec = _security(websocket.app.state)
    if not sec.origin_ok(websocket.headers.get("origin")):
        return WS_POLICY_VIOLATION
    offered = websocket.scope.get("subprotocols") or []
    token = next((p[len(WS_TOKEN_PREFIX) :] for p in offered if p.startswith(WS_TOKEN_PREFIX)),
                 None)  # fmt: skip
    if not sec.token_ok(token):
        return WS_POLICY_VIOLATION
    if open_connections >= sec.max_websockets:
        return WS_TRY_AGAIN_LATER
    return None


def accepted_subprotocol(websocket: WebSocket) -> str | None:
    """Echo ``rq.v1`` when offered (never the token)."""
    offered = websocket.scope.get("subprotocols") or []
    return WS_PROTOCOL if WS_PROTOCOL in offered else None

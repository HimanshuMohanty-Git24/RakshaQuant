"""Shared fixtures for the web tests: a fixed launch token and an authenticated client."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.security import WebSecurity

TOKEN = "test-token-0123456789abcdefghijklmnopqrstuv"
BASE = "http://127.0.0.1:8000"
ORIGIN = BASE
WS_URL = "ws://127.0.0.1:8000/ws"  # websocket_connect ignores base_url
SECURITY = WebSecurity.for_launch("127.0.0.1", 8000, token=TOKEN)
PROTOCOLS = ["rq.v1", f"rq.token.{TOKEN}"]
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def authed(app: FastAPI) -> TestClient:
    """A client on the console's own host, sending the token (as the SPA does)."""
    return TestClient(app, base_url=BASE, headers=AUTH)


def anonymous(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=BASE)

"""Unit coverage for the mounted MCP endpoint's slash behavior."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_mcp_without_trailing_slash_is_gated_without_redirect():
    """The exact mount root must reach MCP auth, not emit an http redirect."""
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/mcp", follow_redirects=False)

    assert response.status_code == 401
    assert response.headers.get("location") is None

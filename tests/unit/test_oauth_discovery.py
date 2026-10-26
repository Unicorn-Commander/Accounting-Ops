"""Contract tests for Accounting-Ops MCP OAuth discovery and HTTP edge behavior."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_settings_parse_expected_audiences_and_mcp_origins(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://accounting-ops.example.com/")
    monkeypatch.setenv(
        "KEYCLOAK_EXPECTED_AUDIENCES", "dynamic-client, another-client"
    )
    monkeypatch.setenv(
        "MCP_ALLOWED_ORIGINS", "https://agent.example.com, https://other.example.com/"
    )
    get_settings.cache_clear()
    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()

    assert settings.keycloak_expected_audiences == (
        "dynamic-client",
        "another-client",
    )
    assert settings.mcp_allowed_origins == (
        "https://accounting-ops.example.com",
        "https://agent.example.com",
        "https://other.example.com",
    )


def test_protected_resource_metadata_is_available_at_both_paths(monkeypatch):
    monkeypatch.setenv(
        "PUBLIC_BASE_URL", "https://accounting-ops.unicorncommander.ai"
    )
    monkeypatch.setenv(
        "KEYCLOAK_REALM_URL", "https://auth.unicorncommander.ai/realms/uchub"
    )
    get_settings.cache_clear()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        responses = [
            client.get("/.well-known/oauth-protected-resource"),
            client.get("/.well-known/oauth-protected-resource/mcp"),
        ]
    finally:
        get_settings.cache_clear()

    for response in responses:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["cache-control"] == "public, max-age=3600"
        assert set(response.json()) == {
            "resource",
            "authorization_servers",
            "bearer_methods_supported",
            "scopes_supported",
            "resource_name",
            "resource_documentation",
        }
        # Clients request exactly these; Keycloak's DCR policy refuses any other scope, and `mcp` carries the audience.
        assert response.json()["scopes_supported"] == ["openid", "profile", "email", "offline_access", "mcp"]
        assert response.json()["resource"] == (
            "https://accounting-ops.unicorncommander.ai/mcp"
        )
        assert response.json()["authorization_servers"] == [
            "https://auth.unicorncommander.ai/realms/uchub"
        ]


def test_protected_resource_options_and_slash_redirect():
    client = TestClient(app, raise_server_exceptions=False)
    options = client.options("/.well-known/oauth-protected-resource/mcp")
    redirect = client.get(
        "/.well-known/oauth-protected-resource/mcp/",
        follow_redirects=False,
    )

    assert options.status_code == 200
    assert options.headers["access-control-allow-origin"] == "*"
    assert set(options.json()) >= {"resource", "authorization_servers"}
    assert redirect.status_code == 308
    assert redirect.headers["location"].endswith(
        "/.well-known/oauth-protected-resource/mcp"
    )


def test_legacy_mcp_manifest_points_at_oauth_discovery():
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/.well-known/mcp.json")

    assert response.status_code == 200
    auth = response.json()["auth"]
    settings = get_settings()
    assert auth["resource"] == f"{settings.public_base_url.rstrip('/')}/mcp"
    assert auth["resource_metadata"].endswith(
        "/.well-known/oauth-protected-resource/mcp"
    )
    assert auth["authorization_servers"] == [str(settings.keycloak_realm_url)]


def test_mcp_challenge_has_resource_metadata_and_never_reflects_token():
    client = TestClient(app, raise_server_exceptions=False)
    missing = client.post("/mcp", json={})
    rejected = client.post(
        "/mcp",
        headers={"Authorization": "Bearer fictional-invalid-token"},
        json={},
    )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith(
        'Bearer realm="accounting-ops-mcp", resource_metadata="'
    )
    assert 'error="invalid_token"' not in missing.headers["www-authenticate"]
    assert rejected.status_code == 401
    assert 'error="invalid_token"' in rejected.headers["www-authenticate"]
    assert "fictional-invalid-token" not in rejected.text


def test_mcp_cors_preflight_and_disallowed_origin_are_before_auth():
    allowed_origin = get_settings().public_base_url.rstrip("/")
    client = TestClient(app, raise_server_exceptions=False)
    preflight = client.options(
        "/mcp",
        headers={
            "Origin": allowed_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    rejected = client.options(
        "/mcp",
        headers={"Origin": "https://blocked.example"},
    )
    browser_unauthorized = client.post(
        "/mcp",
        headers={"Origin": allowed_origin},
        json={},
    )

    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == allowed_origin
    assert "Authorization" in preflight.headers["access-control-allow-headers"]
    assert "Mcp-Session-Id" in preflight.headers["access-control-allow-headers"]
    assert preflight.headers["access-control-expose-headers"] == "Mcp-Session-Id"
    assert rejected.status_code == 403
    assert "www-authenticate" not in rejected.headers
    assert browser_unauthorized.status_code == 401
    assert browser_unauthorized.headers["access-control-allow-origin"] == allowed_origin
    assert browser_unauthorized.headers["access-control-expose-headers"] == "Mcp-Session-Id"


def test_mcp_trailing_slash_redirects_to_canonical_path():
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/mcp/", json={}, follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"].endswith("/mcp")

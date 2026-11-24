"""AO-UP-2: every API response must carry nosniff + CSP + frame-ancestors.

The app served no X-Content-Type-Options / CSP / frame-ancestors anywhere, so
an inline text/markdown or application/xml download could be MIME-sniffed by a
browser into executable content. The middleware stamps all three on every
response.

Offline: a tiny FastAPI app with the middleware + an inline text/markdown
download route; no network.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from app.security_headers import CONTENT_SECURITY_POLICY, SecurityHeadersMiddleware


def _tiny_app() -> Starlette:
    async def download(request) -> Response:
        # The exact shape of the surface the sweep flagged: an inline
        # non-HTML body that a browser could MIME-sniff.
        return PlainTextResponse("# heading\nbody", media_type="text/markdown")

    return Starlette(
        routes=[Route("/dl", download)],
        middleware=[Middleware(SecurityHeadersMiddleware)],
    )


def test_download_carries_security_headers() -> None:
    from starlette.testclient import TestClient

    client = TestClient(_tiny_app())
    resp = client.get("/dl")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_middleware_does_not_clobber_existing_headers() -> None:
    """A route that already sets one of the headers keeps its own value."""

    async def preset(request) -> Response:
        return Response(
            b"{}",
            media_type="application/json",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    app = Starlette(
        routes=[Route("/x", preset)],
        middleware=[Middleware(SecurityHeadersMiddleware)],
    )
    from starlette.testclient import TestClient

    resp = TestClient(app).get("/x")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in resp.headers


def test_main_app_registers_the_middleware() -> None:
    """The real app must mount the middleware (importing app.main is enough:
    the registration runs at module import)."""
    import app.main as main

    from app.security_headers import SecurityHeadersMiddleware

    found = any(mw.cls is SecurityHeadersMiddleware for mw in main.app.user_middleware)
    assert found, "SecurityHeadersMiddleware must be registered on the app"

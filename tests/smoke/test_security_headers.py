"""AO-UP-2: every API response must carry nosniff + CSP + frame-ancestors.

The app served no X-Content-Type-Options / CSP / frame-ancestors anywhere, so
an inline text/markdown or application/xml download could be MIME-sniffed by a
browser into executable content. The middleware stamps all three on every
response.

Offline: a tiny FastAPI app with the middleware + an inline text/markdown
download route; the real app.main.app for the invoice pay page (its invoice
lookup stubbed, no database, lifespan disabled). No network.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from app.security_headers import (
    CONTENT_SECURITY_POLICY,
    INVOICE_HTML_CSP,
    SecurityHeadersMiddleware,
)


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

    found = any(mw.cls is SecurityHeadersMiddleware for mw in main.app.user_middleware)
    assert found, "SecurityHeadersMiddleware must be registered on the app"


def _no_lifespan_client():
    """A TestClient on the real app with the lifespan DISABLED.

    Importing app.main builds the MCP streamable-HTTP app, whose session
    manager only allows ONE lifespan run per process (a second TestClient
    context would raise inside startup). The invoice routes do not use the
    lifespan, so it is safe to skip it here.
    """
    import app.main as main
    from starlette.testclient import TestClient

    client = TestClient(main.app)
    client.__enter__ = lambda self: self
    client.__exit__ = lambda *args: False
    return client


def test_pay_page_serves_html_csp_and_body_is_allowed_by_it(monkeypatch) -> None:
    """R5-P1-1: the public pay page (GET /pay/{token}) renders the brand
    template (inline <style>, style= attributes, Google Fonts, a data: logo).
    The strict API CSP would render that page unstyled, so the route sets
    INVOICE_HTML_CSP and the middleware's setdefault keeps it. The rendered
    body's inline styles, fonts and data: logo must all be allowed by the
    policy the response carries; a JSON route keeps the strict policy.

    The invoice lookup is stubbed (no database): _invoice_for_token returns a
    fixed view, so the route's real response (HTML + headers) is what is
    asserted. Offline.
    """
    from app.api import invoices as invoices_api

    view = {
        "invoice_number": "MU-TEST-1",
        "customer_name": "Test Client",
        "total": "100.00",
        "balance_due": "100.00",
        "currency": "USD",
        "hosted_pay_url": "https://checkout.stripe.com/c/pay/test-token",
        "public_url": "",
        "status": "posted",
    }

    async def _stub(token: str) -> dict:
        return view

    monkeypatch.setattr(invoices_api, "_invoice_for_token", _stub)

    client = _no_lifespan_client()

    resp = client.get("/pay/aaaaaaaaaaaaaaaaaaaaaa")
    assert resp.status_code == 200
    assert resp.headers["Content-Security-Policy"] == INVOICE_HTML_CSP
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    # The page is public and never framed: frame-ancestors 'none'.
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    # The rendered body's resources must all be allowed by the carried policy:
    # inline style block + style= attribute, the Google Fonts stylesheet,
    # the font files, and the data: logo (img-src 'self' data: https:).
    body = resp.text
    assert "<style>" in body
    assert 'style="text-align:right"' in body
    assert "https://fonts.googleapis.com/css2?" in body
    assert "data:image/png;base64," in body
    csp = resp.headers["Content-Security-Policy"]
    assert "style-src 'unsafe-inline' https://fonts.googleapis.com" in csp
    assert "font-src https://fonts.gstatic.com" in csp
    assert "img-src 'self' data: https:" in csp
    # The connected pay button targets the hosted Stripe Checkout URL; the
    # policy's form-action must allow it.
    assert "form-action 'self' https://checkout.stripe.com" in csp
    assert "https://checkout.stripe.com/c/pay/test-token" in body

    # A JSON route keeps the strict default policy (setdefault: the route does
    # not set a CSP).
    resp_json = client.get("/api/openapi.json")
    assert resp_json.status_code == 200
    assert resp_json.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert resp_json.headers["X-Content-Type-Options"] == "nosniff"


def test_pay_page_404_keeps_strict_csp(monkeypatch) -> None:
    """R5-P1-1: an unknown token 404s (JSON) and keeps the strict default CSP,
    not the invoice HTML policy."""
    from app.api import invoices as invoices_api

    async def _stub(token: str) -> dict | None:
        return None

    monkeypatch.setattr(invoices_api, "_invoice_for_token", _stub)

    client = _no_lifespan_client()

    resp = client.get("/pay/aaaaaaaaaaaaaaaaaaaaaa")
    assert resp.status_code == 404
    assert resp.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert resp.headers["X-Content-Type-Options"] == "nosniff"

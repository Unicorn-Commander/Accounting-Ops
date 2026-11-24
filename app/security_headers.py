"""Global security response headers (AO-UP-2).

The API served NO ``X-Content-Type-Options: nosniff`` / CSP / frame-ancestors
anywhere: an inline ``text/markdown`` or ``application/xml`` download could be
MIME-sniffed by a browser into executable content. This middleware stamps the
three headers on every response.

The web app is NOT served by this process (it is a separate front end behind
the same edge), so the DEFAULT policy below is strict: most responses out of
this API are JSON / file downloads with no executable content.

Two routes DO serve HTML to the browser: the public pay page (GET /pay/{token})
and the invoice preview, both rendering ``app/ar/invoice_brand/template.py``
(inline <style>, style= attributes, Google Fonts, a data: logo). A page whose
CSP forbids inline styles renders unstyled, so those routes set the looser
``INVOICE_HTML_CSP`` in the response itself. The middleware uses
``setdefault`` for the CSP, so a per-route header wins and every other
response keeps the strict default. Both the pay page and the preview are
never embedded in a frame, so ``INVOICE_HTML_CSP`` carries
``frame-ancestors 'none'``.

/docs and /redoc are kept (the edge does not block them and the API is the
only surface that documents it); they keep the strict default policy, which
Starlette's own docs page also ships (inline scripts are blocked; the docs UI
degrades but the OpenAPI JSON at /api/openapi.json remains fully available).
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'self'"
)

# The invoice HTML page (pay page + preview) needs exactly what the brand
# template ships: inline <style> + style= attributes, the Google Fonts
# stylesheet and font files, and the data: logo. No scripts anywhere:
# script-src 'none'. Neither the pay page nor the preview is ever embedded in
# a frame, so frame-ancestors 'none'. The template has no forms, so
# form-action is 'none' (the Stripe pay button is a link, not a form submit).
INVOICE_HTML_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https:; "
    "script-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamp nosniff + CSP + frame-ancestors on every response.

    The CSP is setdefault: a route that sets its own Content-Security-Policy
    (the invoice HTML routes use INVOICE_HTML_CSP) keeps it; everything else
    gets the strict default.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response

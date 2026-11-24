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
response keeps the strict default. The pay page is a public link that can sit
in an email and is never embedded in a frame, so the invoice policy carries
``frame-ancestors 'none'``; the preview is same-origin only, so it keeps the
default ``frame-ancestors 'self'``.

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
# stylesheet and font files, the data: logo, and the hosted Stripe Checkout
# link target for a connected pay button (the template has no forms and no
# other external targets). No scripts anywhere: script-src 'none'. The public
# pay page is never embedded in a frame, so frame-ancestors 'none'.
INVOICE_HTML_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https:; "
    "script-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self' https://checkout.stripe.com"
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

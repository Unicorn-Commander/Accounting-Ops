"""Global security response headers (AO-UP-2).

The API served NO ``X-Content-Type-Options: nosniff`` / CSP / frame-ancestors
anywhere: an inline ``text/markdown`` or ``application/xml`` download could be
MIME-sniffed by a browser into executable content. This middleware stamps the
three headers on every response.

The web app is NOT served by this process (it is a separate front end behind
the same edge), so a restrictive ``default-src 'self'`` CSP is safe here: every
response out of this API is JSON / a file download, none is an HTML page the
browser is expected to execute scripts from.
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


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamp nosniff + CSP + frame-ancestors on every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response

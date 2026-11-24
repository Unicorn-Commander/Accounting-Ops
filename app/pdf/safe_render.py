"""One shared safe renderer for every document this service turns into a PDF.

Every document this service turns into a PDF passes through ``render_pdf``.
The renderer uses a URL fetcher that allows only ``data:`` URIs.  A URL from
``file:`` or ``http(s):`` in the HTML would otherwise be followed by
WeasyPrint's default fetcher, and the fetched bytes would land in the PDF the
accountant downloads.  Tenant-written or AI-written drafts can therefore read
local files (environment, keys) or internal URLs into a PDF we hand to a
client.  Refusing everything but ``data:`` URIs makes that impossible.

``markdown_to_html`` escapes raw HTML before Markdown so a draft body cannot
carry an ``<img>``, ``<a rel="attachment">`` or ``<script>`` tag into the
renderer input.  Headings, lists, tables and emphasis all survive.
"""

from __future__ import annotations

import base64
import html as _html
import re

from weasyprint import HTML
from weasyprint.urls import URLFetcher

_ALLOWED_SCHEMES = ("data",)

_TAG_RE = re.compile(r"<[^>]*?>")


class SafeUrlFetcher(URLFetcher):
    """Fetches only ``data:`` URIs and refuses every other scheme.

    ``fetch`` is the entry point WeasyPrint's ``urls.fetch`` context manager
    calls on a fetcher.  A refusal raises a plain exception, which WeasyPrint
    wraps in a ``URLFetchingError`` (treated as a missing resource) because
    ``fail_on_errors`` is False.  No ``file:`` or ``http(s):`` URL is ever
    handed to urllib, so nothing local or internal can be read into a PDF.
    """

    def fetch(self, url, headers=None):  # type: ignore[override]
        if not url.startswith(_ALLOWED_SCHEMES):
            raise ValueError(f"blocked URL: {url}")
        return super().fetch(url, headers)


def safe_url_fetcher() -> SafeUrlFetcher:
    """A WeasyPrint fetcher that refuses file:, http:, https: and all else."""
    return SafeUrlFetcher()


def render_pdf(html: str) -> bytes:
    """Render an HTML string to PDF bytes without fetching anything external.

    ``data:`` URIs (for example the default invoice logo) are allowed.
    Every other scheme is refused; WeasyPrint treats the refusal as a missing
    resource and renders the document without it.
    """
    return HTML(string=html, url_fetcher=safe_url_fetcher()).write_pdf()


def data_uri_for_bytes(raw: bytes, content_type: str) -> str:
    """Encode bytes as a ``data:`` URI for embedding in HTML."""
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _escape_raw_html(md_text: str) -> str:
    """Escape angle brackets inside what looks like an HTML tag.

    ``<script>``, ``<img src=x>`` and ``<a rel="attachment" href=...>`` become
    visible text in the rendered document.  Markdown constructs that are not
    tags (bold, lists, tables, fences) do not start with ``<``, so they pass
    through untouched.
    """

    def _escape(match: re.Match[str]) -> str:
        return _html.escape(match.group(0), quote=False)

    return _TAG_RE.sub(_escape, md_text)


def markdown_to_html(md_text: str) -> str:
    """Convert a Markdown body to an HTML fragment.

    Raw HTML is escaped first (AO-PDF-2): the body is AI or tenant text, and
    passing a raw tag straight into WeasyPrint lets the draft reference
    ``file:`` or ``http:`` URLs.  Headings, lists, tables, emphasis, quotes,
    inline code and fenced code blocks all survive.
    """
    try:
        import markdown
    except ImportError:
        escaped = _html.escape(md_text, quote=False)
        return f"<pre>{escaped}</pre>"

    safe_text = _escape_raw_html(md_text)
    return markdown.markdown(
        safe_text,
        extensions=["tables", "fenced_code", "codehilite", "toc"],
    )

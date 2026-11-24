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

_ALLOWED_SCHEME = "data:"


class SafeUrlFetcher(URLFetcher):
    """Fetches only ``data:`` URIs and refuses every other scheme.

    ``fetch`` is the entry point WeasyPrint's ``urls.fetch`` context manager
    calls on a fetcher.  A refusal raises a plain exception, which WeasyPrint
    wraps in a ``URLFetchingError`` (treated as a missing resource) because
    ``fail_on_errors`` is False.  No ``file:`` or ``http(s):`` URL is ever
    handed to urllib, so nothing local or internal can be read into a PDF.
    The scheme check is exact: the URL must start with ``data:`` (colon
    included), so ``datafile:`` or a relative ``database.css`` is refused.
    """

    def fetch(self, url, headers=None):  # type: ignore[override]
        if not isinstance(url, str) or not url.startswith(_ALLOWED_SCHEME):
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
    """Escape raw HTML in the Markdown source, in two steps.

    Step 1: escape every angle bracket and ampersand with ``html.escape`` so
    no tag can survive and no entity can be forged by the body.

    Step 2: un-escape ``&gt;`` only at the start of a blockquote line (a line
    whose first non-space characters are ``&gt;``).  Python-Markdown's
    blockquote syntax is the one Markdown construct that carries a ``>``
    which must survive; every other ``>`` is either part of an escaped tag
    (now harmless) or a comparison operator.  This restores ``> quoted`` to a
    real blockquote without reopening any raw-HTML channel: a tag at a line
    start (Python-Markdown's raw-HTML block form) has its ``<`` escaped in
    step 1 and is never un-escaped by step 2.
    """
    escaped = _html.escape(md_text, quote=False)
    return re.sub(r"^(\s*)&gt;", r"\1>", escaped, flags=re.MULTILINE)


def markdown_to_html(md_text: str) -> str:
    """Convert a Markdown body to an HTML fragment.

    Raw HTML is escaped first (AO-PDF-2): the body is AI or tenant text, and
    passing a raw tag straight into WeasyPrint lets the draft reference
    ``file:`` or ``http:`` URLs.  ``<script>``, ``<img src=x>`` and
    ``<a rel="attachment" href=...>`` appear as visible text in the renderer
    input.  Headings, lists, tables, emphasis, quotes, inline code and fenced
    code blocks all survive.
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

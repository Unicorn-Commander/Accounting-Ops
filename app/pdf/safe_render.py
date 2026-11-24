"""One shared safe renderer for every document this service turns into a PDF.

Every document this service turns into a PDF passes through ``render_pdf``.
The renderer uses a URL fetcher that allows only ``data:`` URIs.  A URL from
``file:`` or ``http(s):`` in the HTML would otherwise be followed by
WeasyPrint's default fetcher, and the fetched bytes would land in the PDF the
accountant downloads.  Tenant-written or AI-written drafts can therefore read
local files (environment, keys) or internal URLs into a PDF we hand to a
client.  Refusing everything but ``data:`` URIs makes that impossible.

``markdown_to_html`` turns a draft body into an HTML fragment for the renderer
without letting the body carry live URLs or tags in:

* raw HTML tags are escaped outside code spans and fenced code (Python-
  Markdown escapes code itself, so the escape skips code regions and a tag
  inside a fence shows as single-escaped text, not double-escaped);
* after Markdown renders, a post-pass over the generated ``<a href>`` and
  ``<img src>`` attributes allows only ``http``, ``https``, ``mailto`` and
  ``#`` anchors for links and only ``data:`` for images.  A ``javascript:``,
  ``file:`` or ``vbscript:`` URL from the body (inline, reference-style or
  mixed case) is dropped from the attribute, keeping the link text.

Headings, lists, tables and emphasis all survive.
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


# Code spans (single backtick) and fenced code blocks (``` or ~~~) are the
# Markdown constructs Python-Markdown escapes on its own; the raw-HTML escape
# must leave them alone so a tag inside a fence shows once-escaped, not twice.
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_FENCED_CODE_RE = re.compile(r"^\s*(```|~~~)[^\n]*\n.*?^\s*\1[ \t]*$", re.MULTILINE | re.DOTALL)
_BLOCKQUOTE_LINE_RE = re.compile(r"^(\s*)&gt;", re.MULTILINE)
_WHITESPACE_CTRL_RE = re.compile(r"[\x00-\x20\x7f]+")
_ALLOWED_HREF_SCHEMES = ("http:", "https:", "mailto:")
_HREF_ANCHOR = "#"


def _strip_scheme(value: str) -> str:
    """A URL's scheme for allowlist checks, or "" when it has none.

    Control characters and whitespace are stripped first, then the scheme is
    lowercased, so ``JaVaScRiPt:``, ``java\tscript:`` and `` data:`` all
    resolve to their real scheme and a relative URL (``//host/x`` or
    ``page.html``) comes back with an empty scheme.
    """
    cleaned = _WHITESPACE_CTRL_RE.sub("", value)
    match = re.match(r"[a-z0-9][a-z0-9+.-]*:", cleaned, re.IGNORECASE)
    if match is None:
        return ""
    return match.group(0).lower()


def _allow_href(value: str) -> bool:
    """True when a link href may reach the renderer.

    Only ``http``, ``https``, ``mailto`` and ``#`` page anchors are allowed.
    Protocol-relative (``//host``) and relative URLs have no scheme and are
    refused; ``javascript:``, ``file:``, ``vbscript:`` and any other scheme
    are refused case-insensitively after stripping control characters.
    """
    scheme = _strip_scheme(value)
    if scheme == "":
        return value.strip().startswith(_HREF_ANCHOR)
    return scheme in _ALLOWED_HREF_SCHEMES


def _allow_img_src(value: str) -> bool:
    """True when an image src may reach the renderer.

    Only ``data:`` URIs are allowed; everything else (``file:``, ``http:``,
    ``javascript:``, relative, protocol-relative) is dropped.
    """
    return _strip_scheme(value) == _ALLOWED_SCHEME


# A quoted href/src attribute value.  Python-Markdown quotes the value it
# writes (``href="javascript:alert(1)"``); a quoted value that carries an HTML
# entity is escaped text, not a live URL.
_QUOTED_ATTR_RE = re.compile(
    r"((?:href|src)\s*=\s*)([\"'])([^\"']*)\2", re.IGNORECASE
)
# An unquoted href/src attribute value, stopping at the next quote, space or
# tag edge.
_UNQUOTED_ATTR_RE = re.compile(r"((?:href|src)\s*=\s*)([^\"'<>]+)", re.IGNORECASE)


def _drop_unsafe_attr_urls(html_text: str) -> str:
    """Drop unsafe href/src values from generated HTML, keeping the element.

    Runs after Markdown renders (AO107-R4-1): Markdown link and image syntax
    is not raw HTML, so the input escape cannot filter it.  A ``javascript:``
    or ``file:`` URL in an ``<a href>`` or ``<img src>`` is stripped from the
    attribute; the link text and the image alt text stay.  Two phases:

    1. un-quote a quoted value that fails the allowlist (the quotes belong to
       the URL, not to escaped text, so the value can then be checked);
    2. drop the now-unquoted value when it fails the allowlist.

    A quoted value that carries an HTML entity (escaped text from a tag the
    input escape turned into text, or trusted server-side HTML) is inert and
    is kept.
    """

    def _is_live_element(html_text: str, match: re.Match[str]) -> bool:
        """True when the attribute belongs to a real <a> or <img> tag.

        Python-Markdown writes ``<a href=...>`` and ``<img src=...>`` for
        links and images from the body.  A tag the input escape turned into
        text is wrapped in ``<p>`` and carries ``&lt;a`` or ``&lt;img``
        before the attribute, so it is not a live element and is left alone.
        """
        start = match.start()
        window = html_text[max(0, start - 16) : start]
        return "<a" in window or "<img" in window

    def _unquote(match: re.Match[str]) -> str:
        prefix, value = match.group(1), match.group(3)
        if "&" in value:
            return match.group(0)
        if not _is_live_element(html_text, match):
            return match.group(0)
        allowed = _allow_href(value) if "href" in prefix.lower() else _allow_img_src(value)
        if allowed:
            return match.group(0)
        return prefix + value

    html_text = _QUOTED_ATTR_RE.sub(_unquote, html_text)

    def _drop(match: re.Match[str]) -> str:
        prefix, value = match.group(1), match.group(2)
        if not _is_live_element(html_text, match):
            return match.group(0)
        allowed = _allow_href(value) if "href" in prefix.lower() else _allow_img_src(value)
        if allowed:
            return match.group(0)
        return prefix

    return _UNQUOTED_ATTR_RE.sub(_drop, html_text)


def _escape_raw_html(md_text: str) -> str:
    """Escape raw HTML in the Markdown source, outside code regions.

    Step 1: escape every angle bracket and ampersand with ``html.escape`` so
    no tag can survive and no entity can be forged by the body.  Code spans
    and fenced code blocks are left untouched: Python-Markdown escapes the
    code content itself, and escaping it here first would double-escape a tag
    inside a fence to ``&amp;lt;`` (AO107-R4-2).

    Step 2: un-escape ``&gt;`` only at the start of a blockquote line (a line
    whose first non-space characters are ``&gt;``).  Python-Markdown's
    blockquote syntax is the one Markdown construct that carries a ``>``
    which must survive; every other ``>`` is either part of an escaped tag
    (now harmless) or a comparison operator.  This restores ``> quoted`` to a
    real blockquote without reopening any raw-HTML channel: a tag at a line
    start (Python-Markdown's raw-HTML block form) has its ``<`` escaped in
    step 1 and is never un-escaped by step 2.
    """
    code_spans: list[str] = []
    fenced: list[str] = []

    # Stash code regions (fenced blocks first, then spans) under numbered
    # NUL-delimited placeholders, escape everything else, then put the code
    # regions back.  The placeholders are NUL-delimited so no escaped input
    # can contain them, and the escape cannot split a number apart.
    def _stash(text: str, pattern: re.Pattern[str], store: list[str]) -> str:
        def _take(m: re.Match[str]) -> str:
            store.append(m.group(0))
            return f"\x00{len(store) - 1}\x00"

        return pattern.sub(_take, text)

    no_code = _stash(_stash(md_text, _FENCED_CODE_RE, fenced), _CODE_SPAN_RE, code_spans)

    escaped = _html.escape(no_code, quote=False)
    escaped = _BLOCKQUOTE_LINE_RE.sub(r"\1>", escaped)

    for store in (fenced, code_spans):
        for i, region in enumerate(store):
            escaped = escaped.replace(f"\x00{i}\x00", region)
    return escaped


def markdown_to_html(md_text: str) -> str:
    """Convert a Markdown body to an HTML fragment.

    Raw HTML is escaped outside code regions first (AO-PDF-2): the body is AI
    or tenant text, and passing a raw tag straight into WeasyPrint lets the
    draft reference ``file:`` or ``http:`` URLs.  ``<script>``,
    ``<img src=x>`` and ``<a rel="attachment" href=...>`` appear as visible
    text in the renderer input.  Code spans and fenced code are left to
    Python-Markdown's own escaping, so a tag inside a fence shows as text
    exactly once (``&lt;``), not double-escaped (AO107-R4-2).

    After Markdown renders, link and image URLs are filtered (AO107-R4-1):
    Markdown link syntax is not raw HTML, so ``[js](javascript:alert(1))``,
    reference-style definitions and mixed-case schemes would otherwise reach
    the renderer as live href/src values.  Only ``http``, ``https``,
    ``mailto`` and ``#`` anchors survive as link hrefs and only ``data:``
    survives as an image src; every other URL is dropped from the attribute.

    Headings, lists, tables, emphasis, quotes, inline code and fenced code
    blocks all survive.
    """
    try:
        import markdown
    except ImportError:
        escaped = _html.escape(md_text, quote=False)
        return f"<pre>{escaped}</pre>"

    safe_text = _escape_raw_html(md_text)
    html_text = markdown.markdown(
        safe_text,
        extensions=["tables", "fenced_code", "codehilite", "toc"],
    )
    return _drop_unsafe_attr_urls(html_text)

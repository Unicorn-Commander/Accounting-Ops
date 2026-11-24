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

* NUL and C0 control characters are stripped from the body, and Python-
  Markdown runs with its raw-HTML handlers removed, so every ``<`` outside
  code is emitted as ``&lt;`` and code is escaped once, by Markdown itself;
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


# NUL and C0 controls (tab, newline and carriage return are kept).  Python-
# Markdown uses STX/ETX internally as placeholder delimiters, so the body must
# not be able to forge one.
_INPUT_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
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


def _no_raw_html_extension():
    """A Markdown extension that removes Python-Markdown's raw-HTML handlers.

    Without the ``html_block`` preprocessor and the ``html`` inline pattern,
    Markdown treats a tag in the body as text, so the serializer emits every
    ``<`` outside code as ``&lt;``.  Code spans and fenced code are escaped
    once by Markdown itself (no pre-escape, so no ``&amp;lt;``).
    """
    from markdown.extensions import Extension

    class _NoRawHtml(Extension):
        def extendMarkdown(self, md):  # noqa: N802 (Markdown API name)
            md.preprocessors.deregister("html_block", strict=False)
            md.inlinePatterns.deregister("html", strict=False)

    return _NoRawHtml()


def markdown_to_html(md_text: str) -> str:
    """Convert a Markdown body to an HTML fragment.

    The body is AI or tenant text (AO-PDF-2).  NUL and C0 controls are
    stripped, then Markdown runs with its raw-HTML handlers removed
    (AO107-R5-1), so ``<script>``, ``<img src=x>`` and
    ``<a rel="attachment" href=...>`` appear as visible text and code spans
    and fenced code are escaped exactly once, by Markdown.

    After Markdown renders, link and image URLs are filtered (AO107-R4-1).
    Headings, lists, tables, emphasis, quotes, inline code and fenced code
    blocks all survive.
    """
    text = _INPUT_CTRL_RE.sub("", md_text)
    try:
        import markdown
    except ImportError:
        return f"<pre>{_html.escape(text, quote=False)}</pre>"

    html_text = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "toc", _no_raw_html_extension()],
    )
    return _drop_unsafe_attr_urls(html_text)

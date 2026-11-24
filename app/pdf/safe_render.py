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

* NUL and C0 control characters are stripped from the body first;
* Python-Markdown runs with its raw-HTML handlers removed, so every ``<``
  outside code is emitted as ``&lt;`` and code spans and fenced code are
  escaped exactly once, by Markdown itself;
* the generated HTML is then parsed and re-serialized against an allowlist
  of the tags Markdown emits.  Every attribute is dropped except
  ``a[href]`` (``http``, ``https``, ``mailto`` or a ``#`` anchor),
  ``a[title]``, ``img[src]`` (``data:image/*`` only) and ``img[alt]``.  The
  scheme check runs after HTML-unescaping and stripping whitespace and
  control characters, case-insensitively.

Headings, lists, tables and emphasis all survive.
"""

from __future__ import annotations

import base64
import html as _html
import re
from html.parser import HTMLParser

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
_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_ALLOWED_HREF_SCHEMES = ("http:", "https:", "mailto:")
_HREF_ANCHOR = "#"
_IMG_SRC_PREFIX = "data:image/"

# The tags Python-Markdown (with the tables and fenced_code extensions) emits
# for a draft body.  Anything else is dropped; its text content is kept.
_ALLOWED_TAGS = frozenset(
    {
        "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "blockquote", "pre", "code", "em", "strong",
        "a", "img", "table", "thead", "tbody", "tr", "th", "td",
    }
)
_VOID_TAGS = frozenset({"br", "hr", "img"})


def _clean_url(value: str) -> str:
    """The URL with all whitespace and control characters removed.

    The value has already been HTML-unescaped by the parser, so an entity-
    encoded scheme (``jav&#x61;script:``) is compared in its decoded form.
    """
    return _WHITESPACE_CTRL_RE.sub("", value)


def _url_scheme(value: str) -> str:
    """The lowercased scheme of a cleaned URL, or "" when it has none."""
    match = _SCHEME_RE.match(value)
    return match.group(0).lower() if match else ""


def _allow_href(value: str) -> bool:
    """True when a link href may reach the renderer.

    Only ``http``, ``https``, ``mailto`` and ``#`` page anchors.  Protocol-
    relative (``//host``) and relative URLs have no scheme and are refused;
    ``javascript:``, ``file:``, ``vbscript:`` and every other scheme too.
    """
    cleaned = _clean_url(value)
    if cleaned.startswith(_HREF_ANCHOR):
        return True
    return _url_scheme(cleaned) in _ALLOWED_HREF_SCHEMES


def _allow_img_src(value: str) -> bool:
    """True when an image src may reach the renderer: ``data:image/*`` only."""
    return _clean_url(value).lower().startswith(_IMG_SRC_PREFIX)


_ATTR_RULES = {
    "a": {"href": _allow_href, "title": None},
    "img": {"src": _allow_img_src, "alt": None},
}


class _AllowlistSerializer(HTMLParser):
    """Re-serializes generated HTML keeping only allowlisted tags and attrs.

    A real parse, not a regex: the parser decides what is a tag and what is
    an attribute, unescapes entities in attribute values, and every text run
    and kept attribute value is escaped again on the way out.  Comments,
    processing instructions and declarations are dropped.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def _emit_start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _ALLOWED_TAGS:
            return
        rules = _ATTR_RULES.get(tag, {})
        parts = [tag]
        for name, value in attrs:
            if name not in rules or value is None:
                continue
            check = rules[name]
            if check is not None and not check(value):
                continue
            parts.append(f'{name}="{_html.escape(value, quote=True)}"')
        self.out.append("<" + " ".join(parts) + ">")

    def handle_starttag(self, tag, attrs):  # type: ignore[override]
        self._emit_start(tag, attrs)

    def handle_startendtag(self, tag, attrs):  # type: ignore[override]
        self._emit_start(tag, attrs)
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag):  # type: ignore[override]
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):  # type: ignore[override]
        self.out.append(_html.escape(data, quote=False))


def _sanitize_html(html_text: str) -> str:
    """Parse generated HTML and re-serialize it through the allowlist."""
    parser = _AllowlistSerializer()
    parser.feed(html_text)
    parser.close()
    return "".join(parser.out)


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
    """Convert a Markdown body to a safe HTML fragment for the renderer.

    The body is AI or tenant text (AO-PDF-2).  NUL and C0 controls are
    stripped, Markdown runs with its raw-HTML handlers removed, and the
    output is parsed and re-serialized against a tag and attribute allowlist
    (AO107-R5-1, AO107-R5-2).  ``<script>``, ``<img src=x>`` and
    ``<a rel="attachment" href=...>`` appear as visible text; Markdown link
    and image syntax keeps only ``http``/``https``/``mailto``/``#`` hrefs
    and ``data:image/*`` srcs.  Headings, lists, tables, emphasis, quotes,
    inline code and fenced code all survive.
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
    return _sanitize_html(html_text)

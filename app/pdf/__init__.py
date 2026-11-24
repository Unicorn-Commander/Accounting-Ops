"""Shared WeasyPrint rendering for documents built from tenant or AI text.

Every document this service turns into a PDF passes through ``render_pdf``.
The renderer uses a URL fetcher that allows only ``data:`` URIs.  A URL from
``file:`` or ``http(s):`` in the HTML would otherwise be followed by
WeasyPrint's default fetcher, and the fetched bytes would land in the PDF the
accountant downloads.  Tenant-written or AI-written drafts can therefore read
local files (environment, keys) or internal URLs into a PDF we hand to a
client.  Refusing everything but ``data:`` URIs makes that impossible.
"""

from app.pdf.safe_render import (  # noqa: F401
    data_uri_for_bytes,
    markdown_to_html,
    render_pdf,
    safe_url_fetcher,
)

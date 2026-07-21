"""Smoke tests for the regulatory feed fetchers.

We mock httpx so the tests are network-free + deterministic.
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.connectors import regulatory_feeds


# --- Sample RSS payload (truncated, minimum needed to exercise the parser) ---

IRS_RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>IRS Newsroom</title>
    <link>https://www.irs.gov/newsroom</link>
    <description>Newsroom RSS</description>
    <item>
      <title>IRS announces tax inflation adjustments for tax year 2027</title>
      <link>https://www.irs.gov/newsroom/irs-2027-inflation</link>
      <description>Standard deduction rises to $X.</description>
      <guid>https://www.irs.gov/newsroom/irs-2027-inflation</guid>
      <pubDate>Wed, 28 May 2026 16:00:00 -0400</pubDate>
    </item>
    <item>
      <title>Section 174 R&amp;E regulations finalized</title>
      <link>https://www.irs.gov/newsroom/section-174-final</link>
      <description>Final regs on amortisation of research expenditures.</description>
      <guid>https://www.irs.gov/newsroom/section-174-final</guid>
      <pubDate>Tue, 27 May 2026 10:00:00 -0400</pubDate>
    </item>
  </channel>
</rss>
"""


FR_JSON_SAMPLE = """{
  "count": 2,
  "results": [
    {
      "document_number": "2026-14392",
      "title": "Section 174 R&E regulations finalized",
      "abstract": "Final regs on amortisation of research expenditures.",
      "html_url": "https://www.federalregister.gov/documents/2026/07/17/2026-14392/x",
      "publication_date": "2026-07-17",
      "type": "Rule"
    },
    {
      "document_number": "2026-14005",
      "title": "Comment Request on Forms 3520 and 3520-A",
      "abstract": "Agency information collection activities.",
      "html_url": "https://www.federalregister.gov/documents/2026/07/13/2026-14005/y",
      "publication_date": "2026-07-13",
      "type": "Notice"
    }
  ]
}"""

IRS_NEWSROOM_HTML_SAMPLE = (
    '<html><body>'
    # Real news releases — long, sentence-like headlines.
    '<a href="/newsroom/irs-simplifies-penalty-relief-introduces-automatic-process">'
    'IRS simplifies penalty relief, introduces automatic process for eligible taxpayers</a>'
    '<a href="/newsroom/treasury-irs-issue-final-regulations-section-174">'
    'Treasury, IRS issue final regulations on section 174 research expenditures</a>'
    # Navigation chrome that must NOT be ingested as a release.
    '<a href="/newsroom/fact-sheets">Fact sheets</a>'
    '<a href="/newsroom/irs-tax-tips">IRS tax tips</a>'
    '<a href="/newsroom/news-release-and-fact-sheet-archive">'
    'News release and fact sheet archive</a>'
    '<a href="/businesses">Business and self-employed</a>'
    '</body></html>'
)


def _patched_client(text_body: str | None = None, exc: Exception | None = None):
    """Build a patch context for httpx.AsyncClient returning ``text_body``."""
    mock_resp = MagicMock()
    mock_resp.text = text_body
    mock_resp.raise_for_status = MagicMock(return_value=None)

    async def _get(*_args, **_kw):
        if exc is not None:
            raise exc
        return mock_resp

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=_get)

    class _CtxMgr:
        async def __aenter__(self):
            return mock_client
        async def __aexit__(self, *args):
            return False

    return patch.object(
        regulatory_feeds.httpx, "AsyncClient", return_value=_CtxMgr()
    )


@pytest.mark.asyncio
async def test_fetch_irs_federal_register_parses_documents():
    """The IRS regulatory source is now the Federal Register API (JSON).

    irs.gov retired every RSS feed — see the module docstring — so the old
    ``fetch_irs_newsroom_rss`` is gone along with the 404ing URL it wrapped.
    """
    with _patched_client(FR_JSON_SAMPLE):
        entries = await regulatory_feeds.fetch_irs_federal_register()

    assert len(entries) == 2
    assert all(e["source"] == "irs_federal_register" for e in entries)
    assert all(e["jurisdiction"] == "US" for e in entries)
    assert all(isinstance(e["published_at"], dt.datetime) for e in entries)
    titles = [e["title"] for e in entries]
    assert any("Section 174" in t for t in titles)
    # document_number is the stable idempotency key for the ON CONFLICT upsert.
    assert {e["external_id"] for e in entries} == {"2026-14392", "2026-14005"}
    # Document type is preserved in the summary the classifier reads.
    assert any(e["summary"].startswith("Rule:") for e in entries)


@pytest.mark.asyncio
async def test_fetch_irs_newsroom_scrapes_releases_and_skips_navigation():
    with _patched_client(IRS_NEWSROOM_HTML_SAMPLE):
        entries = await regulatory_feeds.fetch_irs_newsroom()

    titles = [e["title"] for e in entries]
    assert any("simplifies penalty relief" in t for t in titles)
    assert any("section 174" in t for t in titles)
    # Section/index pages are not news releases.
    assert not any("Fact sheets" == t for t in titles)
    assert not any("archive" in t.lower() for t in titles)
    assert all(e["source"] == "irs_newsroom" for e in entries)
    assert all(e["url"].startswith("https://www.irs.gov/newsroom/") for e in entries)


@pytest.mark.asyncio
async def test_fetch_raises_rather_than_silently_returning_empty():
    """THE regression guard for the dead-feed defect.

    The old fetcher caught the 404, logged one line, and returned ``[]`` — so a
    dead feed was indistinguishable from a quiet news day and went unnoticed
    for weeks. Fetchers now RAISE so poll_all can record the failure.
    """
    for fetcher in (
        regulatory_feeds.fetch_irs_federal_register,
        regulatory_feeds.fetch_irs_newsroom,
        regulatory_feeds.fetch_scdor_notices,
    ):
        with _patched_client(exc=RuntimeError("404 Not Found")):
            with pytest.raises(regulatory_feeds.FeedFetchError):
                await fetcher()


@pytest.mark.asyncio
async def test_federal_register_malformed_payload_raises():
    with _patched_client('{"unexpected": true}'):
        with pytest.raises(regulatory_feeds.FeedFetchError):
            await regulatory_feeds.fetch_irs_federal_register()

    with _patched_client("not json at all"):
        with pytest.raises(regulatory_feeds.FeedFetchError):
            await regulatory_feeds.fetch_irs_federal_register()


def test_parse_rss_helper_still_handles_real_rss():
    """Retained for future sources that DO publish RSS (state DORs, Treasury)."""
    entries = regulatory_feeds.parse_rss(IRS_RSS_SAMPLE, "some_rss_source")
    assert len(entries) == 2
    assert all(e["source"] == "some_rss_source" for e in entries)
    assert all(isinstance(e["published_at"], dt.datetime) for e in entries)


@pytest.mark.asyncio
async def test_fetch_scdor_notices_extracts_anchor_text():
    html_sample = (
        '<html><body>'
        '<a href="https://dor.sc.gov/communications/notice-2026-01">SC Revenue Ruling #26-1 Notice</a>'
        '<a href="https://dor.sc.gov/about-us">About us</a>'
        '<a href="https://example.com/unrelated">Random link</a>'
        '<a href="/communications/advisory-2026-02">2026 Advisory Opinion 02</a>'
        '</body></html>'
    )
    mock_resp = MagicMock()
    mock_resp.text = html_sample
    mock_resp.raise_for_status = MagicMock(return_value=None)

    async def _get(*_args, **_kw):
        return mock_resp

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=_get)

    class _CtxMgr:
        async def __aenter__(self):
            return mock_client
        async def __aexit__(self, *args):
            return False

    with patch.object(regulatory_feeds.httpx, "AsyncClient", return_value=_CtxMgr()):
        entries = await regulatory_feeds.fetch_scdor_notices()

    # Should pick up the ruling + advisory anchors, not the about page / random link
    titles = [e["title"] for e in entries]
    assert any("Notice" in t for t in titles)
    assert any("Advisory" in t for t in titles)
    assert not any("About us" in t for t in titles)
    assert all(e["jurisdiction"] == "SC" for e in entries)

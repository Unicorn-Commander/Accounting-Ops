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


@pytest.mark.asyncio
async def test_fetch_irs_newsroom_rss_parses_items():
    mock_resp = MagicMock()
    mock_resp.text = IRS_RSS_SAMPLE
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
        entries = await regulatory_feeds.fetch_irs_newsroom_rss()

    assert len(entries) == 2
    titles = [e["title"] for e in entries]
    assert any("inflation" in t for t in titles)
    assert any("Section 174" in t for t in titles)
    assert all(e["source"] == "irs_newsroom_rss" for e in entries)
    assert all(e["jurisdiction"] == "US" for e in entries)
    assert all(isinstance(e["published_at"], dt.datetime) for e in entries)


@pytest.mark.asyncio
async def test_fetch_irs_newsroom_rss_returns_empty_on_http_error():
    async def _get(*_args, **_kw):
        raise RuntimeError("network down")
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=_get)

    class _CtxMgr:
        async def __aenter__(self):
            return mock_client
        async def __aexit__(self, *args):
            return False

    with patch.object(regulatory_feeds.httpx, "AsyncClient", return_value=_CtxMgr()):
        entries = await regulatory_feeds.fetch_irs_newsroom_rss()
    assert entries == []


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

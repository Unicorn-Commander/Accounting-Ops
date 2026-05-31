"""Regulatory feed poller — IRS Newsroom, SCDOR notices, Treasury.

Polls a small set of authoritative regulatory feeds, parses entries, and
upserts new ones into public.regulatory_feed_entry. The Sentinel
``regulatory_updates`` check picks up unclassified rows and per-workspace
decides whether to draft a memo.

Sources (all unauthenticated):
  - IRS Newsroom RSS:
      https://www.irs.gov/newsroom/news-releases-for-current-month
      (RSS feed: https://www.irs.gov/about-irs/irs-newsroom-rss-feeds)
  - SCDOR notices sitemap (no RSS; we fetch the sitemap-ish page and diff).
  - Treasury Federal Register (regulations.gov) — placeholder; defer until
    we have a small JSON endpoint to hit.

Design:
  - Use httpx with the explicit UA pattern from app/auth.py (Cloudflare WAF
    blocks default python-httpx UA).
  - Parse minimal XML (RSS) with stdlib xml.etree.
  - INSERT … ON CONFLICT (source, external_id) DO NOTHING — idempotent.
  - Returns a per-source ingest count for observability.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("acctops.sentinel.regulatory")

USER_AGENT = "accounting-ops-sentinel/0.1 (+https://accounting.magicunicorn.dev)"

IRS_NEWSROOM_RSS = "https://www.irs.gov/newsroom/news-releases-rss"
SCDOR_NOTICES_PAGE = "https://dor.sc.gov/communications"


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


async def fetch_irs_newsroom_rss() -> list[dict[str, Any]]:
    """Fetch + parse the IRS Newsroom RSS. Returns list of entry dicts."""
    out: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(
            timeout=10, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(IRS_NEWSROOM_RSS)
            resp.raise_for_status()
            body = resp.text
    except Exception as e:  # noqa: BLE001
        _log.warning("IRS Newsroom fetch failed: %s", e)
        return out

    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        _log.warning("IRS Newsroom XML parse failed: %s", e)
        return out

    # RSS 2.0: channel/item
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        pub_str = (item.findtext("pubDate") or "").strip()
        pub_at: dt.datetime | None = None
        try:
            # RFC 822: "Wed, 28 May 2026 16:00:00 -0400"
            pub_at = dt.datetime.strptime(pub_str, "%a, %d %b %Y %H:%M:%S %z")
        except (ValueError, TypeError):
            pub_at = None
        if not title or not guid:
            continue
        out.append({
            "source": "irs_newsroom_rss",
            "external_id": guid,
            "title": title[:1000],
            "summary": re.sub(r"<[^>]+>", "", desc)[:2000],
            "url": link,
            "jurisdiction": "US",
            "published_at": pub_at,
        })
    return out


async def fetch_scdor_notices() -> list[dict[str, Any]]:
    """Lightweight scrape of SCDOR communications page → list of entries.

    No RSS — we fetch the HTML index page, regex-match the notice links, and
    use the link href as external_id. Future: a proper HTML parser; for now
    this catches the most-recent ~10 items.
    """
    out: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(
            timeout=10, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(SCDOR_NOTICES_PAGE)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:  # noqa: BLE001
        _log.warning("SCDOR notices fetch failed: %s", e)
        return out

    # Naive: pull <a href="..."> ... </a> entries from the communications page.
    # The site uses a fairly stable card layout; we look for absolute URLs that
    # mention 'communications' or 'tax-notices'.
    for m in re.finditer(
        r'<a\s+[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<text>[^<]+)</a>',
        html,
        flags=re.IGNORECASE,
    ):
        href = m.group("href").strip()
        text_ = m.group("text").strip()
        if not href or len(text_) < 8:
            continue
        if "dor.sc.gov" not in href.lower() and not href.startswith("/"):
            continue
        # Filter for tax-notice / advisory / ruling keywords
        if not any(k in text_.lower() for k in (
            "notice", "ruling", "advisory", "regulation", "bulletin",
        )):
            continue
        full = href if href.startswith("http") else f"https://dor.sc.gov{href}"
        out.append({
            "source": "scdor_notices",
            "external_id": full,
            "title": text_[:300],
            "summary": "",  # we don't crawl deeper
            "url": full,
            "jurisdiction": "SC",
            "published_at": None,
        })
    # De-dup by external_id
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for r in out:
        if r["external_id"] in seen:
            continue
        seen.add(r["external_id"])
        unique.append(r)
    return unique[:50]


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


async def upsert_entries(
    session: AsyncSession, entries: list[dict[str, Any]]
) -> dict[str, int]:
    """Insert new entries; skip on conflict (source, external_id)."""
    if not entries:
        return {"new": 0, "seen": 0}
    new_count = 0
    seen_count = 0
    for e in entries:
        seen_count += 1
        res = await session.execute(
            text(
                """
                INSERT INTO public.regulatory_feed_entry (
                    source, external_id, title, summary, url,
                    jurisdiction, published_at
                ) VALUES (
                    :source, :external_id, :title, :summary, :url,
                    :jurisdiction, :published_at
                )
                ON CONFLICT (source, external_id) DO NOTHING
                """
            ),
            e,
        )
        if (res.rowcount or 0) > 0:
            new_count += 1
    return {"new": new_count, "seen": seen_count}


async def poll_all(session: AsyncSession) -> dict[str, Any]:
    """Poll every configured feed and upsert into the cache. Returns counts."""
    results: dict[str, Any] = {}

    for fetcher_name, fetcher in (
        ("irs_newsroom_rss", fetch_irs_newsroom_rss),
        ("scdor_notices", fetch_scdor_notices),
    ):
        try:
            entries = await fetcher()
            stats = await upsert_entries(session, entries)
            results[fetcher_name] = stats
            _log.info(
                "regulatory_feeds: %s — %d new / %d seen",
                fetcher_name, stats["new"], stats["seen"],
            )
        except Exception as e:  # noqa: BLE001
            _log.warning("regulatory_feeds: %s failed: %s", fetcher_name, e)
            results[fetcher_name] = {"error": str(e)[:200]}
    return results

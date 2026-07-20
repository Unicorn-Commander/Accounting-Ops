"""Regulatory feed poller — IRS (Federal Register + Newsroom), SCDOR.

Polls a small set of authoritative regulatory feeds, parses entries, and
upserts new ones into ``public.regulatory_feed_entry``. The Sentinel
``regulatory_updates`` check picks up unclassified rows and per-workspace
decides whether to draft a memo.

2026-07-20 — the dead IRS feed
==============================
``IRS_NEWSROOM_RSS = "https://www.irs.gov/newsroom/news-releases-rss"`` had been
returning **404** on every cycle for an unknown period. The fetcher swallowed
the error, logged one warning, and returned ``[]``, so the log read
``irs_newsroom_rss — 0 new / 0 seen`` — indistinguishable from "nothing new
today". The ``regulatory_updates`` Sentinel check was therefore silently inert.

Investigation result: **irs.gov no longer publishes ANY RSS/Atom feed.** Every
candidate path 404s (``/newsroom/news-releases-rss``,
``/about-irs/irs-newsroom-rss-feeds``, ``/newsroom/irs-rss-feeds``,
``/rss.xml``, ``/newsroom/feed``, ``/newsroom/irs-newswire``, …), the site's
sitemap set contains zero feed URLs, and the official
``/newsroom/e-news-subscriptions`` page now offers only GovDelivery *email*
subscriptions. The GovDelivery bulletin endpoint
(``content.govdelivery.com/accounts/USIRS/bulletins.rss``) answers **406 Not
Acceptable** to any Accept/User-Agent combination — it is not a public feed.
So there is no drop-in replacement URL; the source had to change.

Replacement sources (both verified returning real, current content)
===================================================================
1. ``irs_federal_register`` — the **Federal Register API**, filtered to the
   Internal Revenue Service. This is the authoritative machine-readable
   publication channel for IRS Rules, Proposed Rules and Notices, and it is a
   documented, versioned, stable JSON API rather than a CMS page that can be
   silently retired. It carries exactly the regulatory material the
   ``regulatory_updates`` classifier looks for (§174, §1202, §163(j) …) — a
   better signal than press releases.
2. ``irs_newsroom`` — an HTML scrape of the IRS "news releases for current
   month" page, which DOES still resolve (200) and still lists the individual
   IR-YYYY-NN releases. Same technique the SCDOR fetcher already uses. This
   preserves the original press-release signal that the dead RSS provided.

Keeping both means one source rotting no longer blinds the check — and the
health table below means we would find out either way.

Health signal (the actual fix)
==============================
Every fetch now records into ``public.regulatory_feed_health``: attempt time,
success time, consecutive failure count, last error, last entry count. The
Sentinel ``regulatory_updates`` check reads that table and raises a
``regulatory_feed_stale`` finding when a feed has been failing past threshold —
so the next dead feed surfaces to a human instead of rotting in a log file.
``FEED_STALE_AFTER_HOURS`` / ``FEED_STALE_AFTER_FAILURES`` tune the thresholds.

Design:
  - httpx with the explicit UA pattern from app/auth.py (Cloudflare WAF blocks
    the default python-httpx UA).
  - Parse RSS/XML with stdlib xml.etree; JSON with stdlib json.
  - INSERT … ON CONFLICT (source, external_id) DO NOTHING — idempotent.
  - Returns a per-source ingest count for observability.
"""
from __future__ import annotations

import datetime as dt
import html as _html
import logging
import os
import re
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("acctops.sentinel.regulatory")

USER_AGENT = "accounting-ops-sentinel/0.1 (+https://accounting.magicunicorn.dev)"

# Federal Register API, filtered to the IRS. Verified 2026-07-20: HTTP 200,
# application/json, current documents. Overridable so an operator can retarget
# (or add agencies) without a code change.
IRS_FEDERAL_REGISTER_API = os.getenv(
    "REGULATORY_IRS_FR_URL",
    "https://www.federalregister.gov/api/v1/documents.json"
    "?conditions%5Bagencies%5D%5B%5D=internal-revenue-service"
    "&order=newest&per_page=40"
    "&fields%5B%5D=document_number&fields%5B%5D=title&fields%5B%5D=abstract"
    "&fields%5B%5D=html_url&fields%5B%5D=publication_date&fields%5B%5D=type",
)

# IRS newsroom index — HTML, no RSS. Verified 2026-07-20: HTTP 200 and lists
# the current month's IR-YYYY-NN releases as /newsroom/<slug> links.
IRS_NEWSROOM_PAGE = os.getenv(
    "REGULATORY_IRS_NEWSROOM_URL",
    "https://www.irs.gov/newsroom/news-releases-for-current-month",
)

SCDOR_NOTICES_PAGE = os.getenv(
    "REGULATORY_SCDOR_URL", "https://dor.sc.gov/communications"
)

# A feed is "stale" (-> Sentinel finding) once BOTH the failure count and the
# time-since-success thresholds are meaningful. See feed_health_report().
FEED_STALE_AFTER_HOURS = int(os.getenv("REGULATORY_FEED_STALE_HOURS", "48"))
FEED_STALE_AFTER_FAILURES = int(os.getenv("REGULATORY_FEED_STALE_FAILURES", "3"))


class FeedFetchError(RuntimeError):
    """Raised by a fetcher when the source could not be read.

    Exists so ``poll_all`` can tell "the feed is broken" apart from "the feed
    is fine and had nothing new". The old code collapsed both into ``[]``,
    which is precisely how a 404 went unnoticed for weeks.
    """


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


async def _get(url: str, *, accept: str) -> str:
    async with httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": USER_AGENT, "Accept": accept},
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def fetch_irs_federal_register() -> list[dict[str, Any]]:
    """IRS documents from the Federal Register API (JSON). Raises FeedFetchError."""
    import json

    try:
        body = await _get(IRS_FEDERAL_REGISTER_API, accept="application/json")
    except Exception as e:  # noqa: BLE001
        raise FeedFetchError(f"fetch failed: {e}") from e

    try:
        payload = json.loads(body)
    except ValueError as e:
        raise FeedFetchError(f"JSON parse failed: {e}") from e

    results = payload.get("results")
    if not isinstance(results, list):
        raise FeedFetchError("unexpected payload: no 'results' array")

    out: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        doc_no = str(item.get("document_number") or "").strip()
        title = str(item.get("title") or "").strip()
        url = str(item.get("html_url") or "").strip()
        if not title or not (doc_no or url):
            continue
        pub_at: dt.datetime | None = None
        pub_raw = str(item.get("publication_date") or "").strip()
        if pub_raw:
            try:
                pub_at = dt.datetime.strptime(pub_raw, "%Y-%m-%d").replace(
                    tzinfo=dt.timezone.utc
                )
            except ValueError:
                pub_at = None
        doc_type = str(item.get("type") or "").strip()
        abstract = str(item.get("abstract") or "").strip()
        summary = f"{doc_type}: {abstract}" if doc_type and abstract else (abstract or doc_type)
        out.append({
            "source": "irs_federal_register",
            "external_id": doc_no or url,
            "title": title[:1000],
            "summary": summary[:2000],
            "url": url,
            "jurisdiction": "US",
            "published_at": pub_at,
        })
    return out


async def fetch_irs_newsroom() -> list[dict[str, Any]]:
    """IRS news releases scraped from the newsroom index. Raises FeedFetchError.

    irs.gov publishes no feed (see module docstring), so we diff the HTML index
    the same way ``fetch_scdor_notices`` does. Individual releases live at
    ``/newsroom/<slug>``; the page's own navigation links share that prefix, so
    we exclude the known section pages by slug rather than guessing from
    structure.
    """
    try:
        page = await _get(IRS_NEWSROOM_PAGE, accept="text/html")
    except Exception as e:  # noqa: BLE001
        raise FeedFetchError(f"fetch failed: {e}") from e

    # Section/navigation pages under /newsroom/ that are NOT news releases.
    nav_slugs = {
        "fact-sheets", "irs-guidance", "irs-statements-and-announcements",
        "irs-tax-tips", "multimedia-center", "news-releases-for-current-month",
        "news-releases-for-frequently-asked-questions", "posters-to-share",
        "tax-articles-to-share", "tax-relief-in-disaster-situations",
        "topics-in-the-news", "news-release-and-fact-sheet-archive",
        "working-families-tax-cuts", "e-news-subscriptions",
        "general-overview-of-taxpayer-reliance-on-guidance-published-in-the-"
        "internal-revenue-bulletin-and-faqs",
    }

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'<a\s+[^>]*href="(?P<href>/newsroom/[^"#?]+)"[^>]*>(?P<text>.*?)</a>',
        page,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        href = m.group("href").strip().rstrip("/")
        slug = href.rsplit("/", 1)[-1].lower()
        if slug in nav_slugs:
            continue
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group("text")))
        title = re.sub(r"\s+", " ", title).strip()
        # Real release headlines are sentences; nav chrome is short.
        if len(title) < 25:
            continue
        full = f"https://www.irs.gov{href}"
        if full in seen:
            continue
        seen.add(full)
        out.append({
            "source": "irs_newsroom",
            "external_id": full,
            "title": title[:1000],
            "summary": "",  # we don't crawl into each release
            "url": full,
            "jurisdiction": "US",
            "published_at": None,
        })
    return out[:50]


async def fetch_scdor_notices() -> list[dict[str, Any]]:
    """Lightweight scrape of SCDOR communications page → list of entries.

    No RSS — we fetch the HTML index page, regex-match the notice links, and
    use the link href as external_id. Future: a proper HTML parser; for now
    this catches the most-recent ~10 items.
    """
    try:
        html = await _get(SCDOR_NOTICES_PAGE, accept="text/html")
    except Exception as e:  # noqa: BLE001
        raise FeedFetchError(f"fetch failed: {e}") from e

    out: list[dict[str, Any]] = []
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


def parse_rss(body: str, source: str, jurisdiction: str = "US") -> list[dict[str, Any]]:
    """Parse an RSS 2.0 document into entry dicts.

    Retained as a reusable helper (and used by the generic RSS path) now that
    the IRS-specific RSS fetcher is gone — several candidate future sources
    (state DORs, Treasury) still publish real RSS.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise FeedFetchError(f"XML parse failed: {e}") from e

    out: list[dict[str, Any]] = []
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
            "source": source,
            "external_id": guid,
            "title": title[:1000],
            "summary": re.sub(r"<[^>]+>", "", desc)[:2000],
            "url": link,
            "jurisdiction": jurisdiction,
            "published_at": pub_at,
        })
    return out


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


# ---------------------------------------------------------------------------
# Health tracking
# ---------------------------------------------------------------------------


async def record_feed_health(
    session: AsyncSession,
    source: str,
    *,
    ok: bool,
    entry_count: int = 0,
    error: str | None = None,
) -> None:
    """Upsert one row of ``public.regulatory_feed_health``.

    Best-effort: a missing table (deploy landed before migration 0014) logs and
    returns rather than breaking the poll. Health tracking must never be the
    reason the feeds stop being polled.
    """
    try:
        await session.execute(
            text(
                """
                INSERT INTO public.regulatory_feed_health (
                    source, last_attempt_at, last_success_at,
                    consecutive_failures, last_error, last_entry_count, updated_at
                ) VALUES (
                    :source, now(),
                    CASE WHEN :ok THEN now() ELSE NULL END,
                    CASE WHEN :ok THEN 0 ELSE 1 END,
                    :err,
                    :entries,
                    now()
                )
                ON CONFLICT (source) DO UPDATE SET
                    last_attempt_at = now(),
                    last_success_at = CASE
                        WHEN :ok THEN now()
                        ELSE regulatory_feed_health.last_success_at
                    END,
                    consecutive_failures = CASE
                        WHEN :ok THEN 0
                        ELSE regulatory_feed_health.consecutive_failures + 1
                    END,
                    last_error = CASE WHEN :ok THEN NULL ELSE :err END,
                    last_entry_count = CASE
                        WHEN :ok THEN :entries
                        ELSE regulatory_feed_health.last_entry_count
                    END,
                    updated_at = now()
                """
            ),
            {
                "source": source,
                "ok": ok,
                "err": (error or "")[:2000] or None,
                "entries": int(entry_count or 0),
            },
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("feed health upsert failed for %s: %s", source, e)


async def feed_health_report(
    session: AsyncSession,
    *,
    stale_after_hours: int | None = None,
    stale_after_failures: int | None = None,
) -> list[dict[str, Any]]:
    """Return one dict per feed source with a computed ``stale`` verdict.

    A feed is STALE when it has failed at least ``stale_after_failures`` times
    in a row AND has not succeeded within ``stale_after_hours`` (or has never
    succeeded at all). Requiring both avoids paging on a single transient
    network blip while still catching a URL that has rotted away.
    """
    hours = FEED_STALE_AFTER_HOURS if stale_after_hours is None else stale_after_hours
    fails = (
        FEED_STALE_AFTER_FAILURES
        if stale_after_failures is None
        else stale_after_failures
    )
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT source, last_attempt_at, last_success_at,
                           consecutive_failures, last_error, last_entry_count,
                           EXTRACT(EPOCH FROM (now() - last_success_at)) / 3600.0
                               AS hours_since_success
                    FROM public.regulatory_feed_health
                    ORDER BY source
                    """
                )
            )
        ).mappings().all()
    except Exception as e:  # noqa: BLE001
        _log.warning("feed health read failed: %s", e)
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        hours_since = r["hours_since_success"]
        never_succeeded = r["last_success_at"] is None
        overdue = never_succeeded or (
            hours_since is not None and float(hours_since) >= hours
        )
        stale = bool(int(r["consecutive_failures"] or 0) >= fails and overdue)
        out.append({
            "source": r["source"],
            "last_attempt_at": r["last_attempt_at"],
            "last_success_at": r["last_success_at"],
            "consecutive_failures": int(r["consecutive_failures"] or 0),
            "last_error": r["last_error"],
            "last_entry_count": int(r["last_entry_count"] or 0),
            "hours_since_success": (
                None if hours_since is None else round(float(hours_since), 1)
            ),
            "never_succeeded": never_succeeded,
            "stale": stale,
        })
    return out


# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------

# (slug, fetcher). Order is cosmetic — each is independent.
FEEDS = (
    ("irs_federal_register", fetch_irs_federal_register),
    ("irs_newsroom", fetch_irs_newsroom),
    ("scdor_notices", fetch_scdor_notices),
)


async def poll_all(session: AsyncSession) -> dict[str, Any]:
    """Poll every configured feed and upsert into the cache. Returns counts.

    Each source's outcome is recorded to ``regulatory_feed_health`` whether it
    succeeded or failed — that record is what makes a dead feed detectable.
    A source that raises is isolated: the others still poll.
    """
    results: dict[str, Any] = {}

    for source, fetcher in FEEDS:
        try:
            entries = await fetcher()
        except FeedFetchError as e:
            _log.warning("regulatory_feeds: %s FAILED: %s", source, e)
            await record_feed_health(session, source, ok=False, error=str(e))
            results[source] = {"error": str(e)[:200], "ok": False}
            continue
        except Exception as e:  # noqa: BLE001
            _log.exception("regulatory_feeds: %s crashed: %s", source, e)
            await record_feed_health(
                session, source, ok=False, error=f"{type(e).__name__}: {e}"
            )
            results[source] = {"error": str(e)[:200], "ok": False}
            continue

        try:
            stats = await upsert_entries(session, entries)
        except Exception as e:  # noqa: BLE001
            _log.exception("regulatory_feeds: %s upsert failed: %s", source, e)
            await record_feed_health(
                session, source, ok=False, error=f"upsert: {type(e).__name__}: {e}"
            )
            results[source] = {"error": str(e)[:200], "ok": False}
            continue

        # A 200 that yields zero parseable entries is treated as a FAILURE, not
        # a quiet success: it is the exact shape a silently-retired feed takes
        # (the page still resolves, the content is gone). Requiring
        # consecutive_failures >= threshold keeps a genuinely empty news day
        # from raising an alarm.
        if not entries:
            _log.warning(
                "regulatory_feeds: %s returned 0 entries (reachable but empty)",
                source,
            )
            await record_feed_health(
                session, source, ok=False, error="fetch returned 0 entries"
            )
            results[source] = {**stats, "ok": False, "warning": "0 entries"}
            continue

        await record_feed_health(
            session, source, ok=True, entry_count=len(entries)
        )
        results[source] = {**stats, "ok": True}
        _log.info(
            "regulatory_feeds: %s — %d new / %d seen",
            source, stats["new"], stats["seen"],
        )

    return results

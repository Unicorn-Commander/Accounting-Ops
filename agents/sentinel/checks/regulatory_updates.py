"""Regulatory updates check.

Reads unclassified rows from public.regulatory_feed_entry (populated by
app.connectors.regulatory_feeds in a periodic side task). For each new entry
runs a lightweight classifier: does it mention C-Corp / SC / §174 / §1202 /
estimated-tax / 1099-NEC keywords this entity cares about? If yes, draft a
memo in the workspace's tax_review_queue.

Feed health (added 2026-07-20)
==============================
This check is the CONSUMER of the regulatory feeds, so it is also where a dead
feed has to become visible. The IRS Newsroom RSS URL 404'd for weeks while this
check reported a healthy "no new entries" every cycle — because an unreachable
feed and a quiet news day produce byte-identical output here: zero rows.

``_check_feed_health`` runs FIRST and, critically, runs BEFORE the
no-new-entries early return. A dead feed produces no entries by definition, so
health checking placed after that return would be unreachable in exactly the
situation it exists to catch.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities
from app.connectors.regulatory_feeds import feed_health_report

# Keyword vocabulary keyed by signal name. Lower-cased; sub-string match.
KEYWORD_SIGNALS = {
    "c_corp":         ["c corporation", "c-corp", "form 1120", "corporate income tax"],
    "s_corp":         ["s corporation", "s-corp", "form 1120-s"],
    "sec_174":        ["section 174", "§174", "research and experimental", "r&e expenses"],
    "sec_195":        ["section 195", "§195", "startup costs", "start-up expenditures"],
    "sec_1202":       ["section 1202", "§1202", "qualified small business stock", "qsbs"],
    "sec_163j":       ["section 163(j)", "§163(j)", "interest deductibility limit"],
    "estimated_tax":  ["estimated tax", "form 1120-w", "quarterly payment"],
    "1099_nec":       ["form 1099-nec", "1099-nec", "non-employee compensation"],
    "officer_comp":   ["officer compensation", "reasonable compensation"],
    "sc_state":       ["south carolina", "scdor", "sc1120"],
    "nol":            ["net operating loss", "nol", "carryforward"],
}


def _classify_entry(title: str, summary: str) -> dict[str, Any]:
    """Compute signal tags + jurisdiction guess from text."""
    haystack = f"{title} {summary}".lower()
    matched: list[str] = []
    for signal, words in KEYWORD_SIGNALS.items():
        for w in words:
            if w in haystack:
                matched.append(signal)
                break
    jurisdiction = "SC" if "sc_state" in matched else "US"
    return {"signals": sorted(set(matched)), "jurisdiction": jurisdiction}


def _entity_signals(entity_kind: str, country_code: str) -> set[str]:
    """Which signals does this entity care about?"""
    sig = {"estimated_tax", "1099_nec"}
    if entity_kind == "c_corp":
        sig.update({"c_corp", "sec_174", "sec_195", "sec_1202", "sec_163j", "officer_comp", "nol"})
    if entity_kind == "s_corp":
        sig.update({"s_corp", "sec_174", "officer_comp", "nol"})
    if country_code == "US":
        sig.add("sc_state")  # we conservatively send SC notices to all US entities
    return sig


async def _fetch_unclassified(session) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT id::text, source, external_id, title, summary, url,
                       jurisdiction, published_at
                FROM public.regulatory_feed_entry
                WHERE classified = false
                ORDER BY first_seen_at DESC
                LIMIT 50
                """
            )
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def _mark_classified(session, entry_ids: list[str], tags: dict[str, dict]) -> None:
    if not entry_ids:
        return
    for eid in entry_ids:
        t = tags.get(eid, {})
        await session.execute(
            text(
                """
                UPDATE public.regulatory_feed_entry
                SET classified = true,
                    jurisdiction = COALESCE(jurisdiction, :jur),
                    tags = COALESCE(tags, '{}'::jsonb) ||
                           CAST(:sig AS jsonb)
                WHERE id = :id
                """
            ),
            {
                "id": eid,
                "jur": t.get("jurisdiction"),
                "sig": __import__("json").dumps({"signals": t.get("signals", [])}),
            },
        )


async def _check_feed_health(ctx: ActionContext) -> dict[str, int]:
    """Raise a finding for any regulatory feed that has gone persistently dead.

    Severity is ``warning``: the workspace's regulatory monitoring has a blind
    spot, which is a data-completeness caveat on every "no new updates" result
    this check has produced since the feed died — a CPA needs to know that.

    One finding PER SOURCE (dedupe_key includes the slug) so a dead IRS feed
    and a dead SCDOR feed don't collapse into one row and mask each other.
    ``log_finding`` is the action type because it is available at
    ``watch_only`` — a workspace that has deliberately limited Sentinel to
    observation still needs to be told its inputs are broken.
    """
    findings = 0
    actions = 0
    for feed in await feed_health_report(ctx.session):
        if not feed.get("stale"):
            continue
        source = feed["source"]
        if feed["never_succeeded"]:
            age = "has NEVER successfully fetched"
        else:
            age = f"has not succeeded in {feed['hours_since_success']:.0f}h"
        summary = (
            f"Regulatory feed '{source}' {age} "
            f"({feed['consecutive_failures']} consecutive failures). "
            f"Regulatory monitoring that depends on this source is INERT: "
            f"'no new updates' results are not trustworthy until it is fixed. "
            f"Last error: {feed['last_error'] or 'n/a'}"
        )
        res = await execute_action(
            ctx,
            action_type="log_finding",
            entity_id=None,
            title=f"Regulatory feed '{source}' is not returning data",
            summary=summary,
            severity="warning",
            details={
                "feed_source": source,
                "consecutive_failures": feed["consecutive_failures"],
                "last_success_at": (
                    feed["last_success_at"].isoformat()
                    if feed["last_success_at"] else None
                ),
                "hours_since_success": feed["hours_since_success"],
                "last_error": feed["last_error"],
                "remediation": (
                    "Verify the feed URL still resolves and returns parseable "
                    "content. Sources are env-overridable: REGULATORY_IRS_FR_URL, "
                    "REGULATORY_IRS_NEWSROOM_URL, REGULATORY_SCDOR_URL."
                ),
            },
            dedupe_key=f"regulatory_feed_stale:{source}",
            reasoning=(
                f"regulatory_feed_health.{source}: "
                f"{feed['consecutive_failures']} consecutive failures and no "
                f"success within the staleness window."
            ),
        )
        findings += 1
        if res.get("ok"):
            actions += 1
    return {"findings": findings, "actions": actions}


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    # 0. Feed health FIRST — before the no-entries early return below, because
    #    a dead feed IS the no-entries case and would otherwise never surface.
    health = await _check_feed_health(ctx)
    health_findings = health["findings"]
    health_actions = health["actions"]

    # 1. Fetch unclassified entries (workspace-shared cache in public schema)
    entries = await _fetch_unclassified(ctx.session)
    if not entries:
        stale_note = (
            f" ({health_findings} feed(s) FLAGGED STALE)" if health_findings else ""
        )
        return {
            "findings": health_findings,
            "actions": health_actions,
            "summary": f"regulatory_updates: no new entries{stale_note}",
        }

    entities = await list_active_entities(ctx.session)
    if not entities:
        # Mark them classified anyway so we don't reprocess them every cycle.
        await _mark_classified(
            ctx.session, [e["id"] for e in entries],
            {e["id"]: _classify_entry(e["title"] or "", e["summary"] or "") for e in entries},
        )
        return {
            "findings": health_findings,
            "actions": health_actions,
            "summary": "regulatory_updates: no entities",
        }

    findings = health_findings
    actions = health_actions
    classification: dict[str, dict] = {}
    for entry in entries:
        cls = _classify_entry(entry["title"] or "", entry["summary"] or "")
        classification[entry["id"]] = cls
        if not cls["signals"]:
            continue
        # For each entity, if its interest set intersects the signals → memo it.
        for ent in entities:
            wants = _entity_signals(ent.kind, ent.country_code)
            relevant = wants.intersection(cls["signals"])
            if not relevant:
                continue
            topic = f"sentinel-reg-{entry['source']}-{entry['external_id'][:60]}"
            title = f"[{cls['jurisdiction']}] {entry['title'][:140]}"
            summary = (
                f"Regulatory update relevant to {ent.display_name} "
                f"({', '.join(sorted(relevant))}). "
                f"{(entry['summary'] or '')[:400]}"
            )
            if entry.get("url"):
                summary += f"\n\nSource: {entry['url']}"
            res = await execute_action(
                ctx,
                action_type="draft_memo",
                entity_id=ent.id,
                title=title,
                summary=summary,
                severity="info",
                details={
                    "feed_source": entry["source"],
                    "feed_external_id": entry["external_id"],
                    "signals": sorted(relevant),
                    "jurisdiction": cls["jurisdiction"],
                    "url": entry.get("url"),
                },
                payload={
                    "tax_year": dt.date.today().year,
                    "topic": topic,
                    "confidence": "medium",
                    "citations": [{"type": "url", "value": entry.get("url")}] if entry.get("url") else [],
                },
                # One open finding per regulatory item — multiple per entity.
                dedupe_key=topic,
                reasoning=(
                    f"Regulatory update from {entry['source']} matched signals "
                    f"{sorted(relevant)} for {ent.slug}."
                ),
            )
            findings += 1
            if res.get("ok"):
                actions += 1

    # Mark every fetched entry classified (even ones with no signal — we don't
    # want to keep re-scanning them every cycle).
    await _mark_classified(ctx.session, [e["id"] for e in entries], classification)

    stale_note = f", {health_findings} feed(s) FLAGGED STALE" if health_findings else ""
    return {
        "findings": findings,
        "actions": actions,
        "summary": (
            f"regulatory_updates: {len(entries)} entries scanned, "
            f"{findings - health_findings} relevant findings{stale_note}"
        ),
    }

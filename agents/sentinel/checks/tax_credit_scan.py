"""Tax credit & grant scanner.

Static catalog of programs Sentinel knows to look at; matches them against
each entity's profile (kind, country, fiscal year stage). For matches, draft a
memo in the queue with the application deadline and a link.

This is a deliberately conservative first cut: a curated list rather than a
live scrape, because false positives here cost CPA review time. The list can
be extended without code changes by adding to ``_CATALOG``.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities

# Each entry: a static credit/grant program. ``applies(entity)`` filters by
# entity type / country / sometimes fiscal-year stage.
_CATALOG: list[dict[str, Any]] = [
    {
        "name": "Federal R&D Tax Credit (Form 6765)",
        "url": "https://www.irs.gov/businesses/research-credit",
        "applies_kinds": {"c_corp", "s_corp", "llc", "partnership"},
        "applies_countries": {"US"},
        "summary": (
            "If the entity has §174 R&E expenses (software dev, etc.), it likely "
            "also qualifies for the §41 federal R&D credit. The Alternative "
            "Simplified Credit (ASC) is ~14% of QREs over a base. Worth "
            "evaluating for any entity capitalising R&E under §174 amortisation."
        ),
        "form": "6765",
    },
    {
        "name": "QSBS §1202 Exclusion (eligibility tracking)",
        "url": "https://www.irs.gov/forms-pubs/about-form-8949",
        "applies_kinds": {"c_corp"},
        "applies_countries": {"US"},
        "summary": (
            "C-Corp founders + early investors holding shares >5 years may exclude "
            "up to $10M of gains on sale under §1202. Track holding periods + "
            "ensure the entity meets the 80% active-business test."
        ),
        "form": "8949",
    },
    {
        "name": "SC Job Tax Credit",
        "url": "https://dor.sc.gov/tax-credits/job-tax-credit",
        "applies_kinds": {"c_corp", "s_corp", "llc"},
        "applies_countries": {"US"},
        "summary": (
            "South Carolina employers creating qualifying jobs in designated "
            "counties may claim up to $25k/job/year for 5 years. Worth checking "
            "if the entity hired in SC during the year."
        ),
        "form": "TC-4",
    },
    {
        "name": "Work Opportunity Tax Credit (WOTC, Form 8850)",
        "url": "https://www.irs.gov/forms-pubs/about-form-5884",
        "applies_kinds": {"c_corp", "s_corp", "llc", "partnership"},
        "applies_countries": {"US"},
        "summary": (
            "Federal credit for hiring members of targeted groups (veterans, "
            "long-term unemployed, etc.). Form 8850 must be filed within 28 days "
            "of hire — easy to miss after the fact."
        ),
        "form": "8850",
    },
    {
        "name": "Energy-Efficient Commercial Buildings Deduction (§179D)",
        "url": "https://www.irs.gov/credits-deductions/energy-efficient-commercial-buildings-deduction",
        "applies_kinds": {"c_corp", "s_corp", "llc"},
        "applies_countries": {"US"},
        "summary": (
            "§179D allows a deduction (up to ~$5/sqft) for energy-efficient "
            "lighting/HVAC/envelope improvements on commercial buildings. Worth a "
            "look if the entity owns/leases commercial property + did upgrades."
        ),
        "form": "7205",
    },
]


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0
    today = dt.date.today()

    # We only want to surface each program once per entity per CALENDAR QUARTER —
    # use a topic that bakes in the current YYYY-Q.
    quarter = (today.month - 1) // 3 + 1
    quarter_key = f"{today.year}Q{quarter}"

    for ent in entities:
        for program in _CATALOG:
            if ent.kind not in program["applies_kinds"]:
                continue
            if ent.country_code not in program["applies_countries"]:
                continue

            slug = program["name"].lower().replace(" ", "-").replace("(", "").replace(")", "")[:50]
            topic = f"sentinel-credit-{slug}-{quarter_key}"

            title = f"Possibly applicable: {program['name']}"
            summary = (
                f"{program['summary']} For {ent.display_name} ({ent.kind.upper()}), "
                f"this may apply for the {ent.fiscal_year_start_month}/{today.year} "
                f"fiscal year. Worth evaluating with the CPA."
            )

            res = await execute_action(
                ctx,
                action_type="draft_memo",
                entity_id=ent.id,
                title=title,
                summary=summary,
                severity="info",
                details={
                    "program": program["name"],
                    "form": program.get("form"),
                    "url": program["url"],
                    "quarter": quarter_key,
                },
                payload={
                    "tax_year": today.year,
                    "topic": topic,
                    "confidence": "low",  # always low — needs CPA judgment
                    "citations": [{"type": "url", "value": program["url"]}],
                },
                reasoning=(
                    f"Tax credit scanner: {program['name']} potentially applies to "
                    f"{ent.slug} ({ent.kind})."
                ),
            )
            findings += 1
            if res.get("ok"):
                actions += 1

    return {
        "findings": findings,
        "actions": actions,
        "summary": f"tax_credit_scan: {findings} potential credits surfaced",
    }

"""Qwen-vs-frontier evaluation harness for the categorizer (agent #1).

The decider for "can the local Qwen do this agent's specific job as well as a
frontier model?" — run real transactions through BOTH the local Qwen
(qwen3.6-35b-a3b) and a frontier model (accounting-ops-sonnet) using the EXACT
production categorizer prompt, then report exact-category agreement + the
disagreements to adjudicate. Read-only: writes nothing to the books.

This is the reusable template for evaluating any agent before flipping its
routing policy from frontier to Qwen — measure on real data, don't guess.

Usage (inside the app container, where both gateway keys + the DB are):

    docker exec -i accounting_ops-app-1 python - < scripts/eval_categorizer.py

Env overrides:
    EVAL_WORKSPACE_ID   workspace UUID to sample uncategorized txns from
    EVAL_N              number of transactions to evaluate (default 40)
    EVAL_FRONTIER_MODEL frontier model alias on the cloud gateway
                        (default accounting-ops-sonnet)

Result (printed): agreement %, how often each model abstained, and every
disagreement with both models' answers + confidences for adjudication.
"""
import asyncio
import json
import os
import time
from collections import Counter
from decimal import Decimal
from uuid import UUID

import httpx
from sqlalchemy import text

from app.categorization.llm import (
    _SYSTEM_PROMPT, _FEW_SHOT_EXAMPLES, _build_user_message,
    _extract_completion, _parse_and_validate,
)
from app.config import get_settings
from app.db import SessionLocal
from app.journal.posting import CATEGORY_TO_ACCOUNT
from app.workspace_session import workspace_session

WS = UUID(os.getenv("EVAL_WORKSPACE_ID", "77753ea5-efe9-5539-a846-6b20ddc841b9"))
N = int(os.getenv("EVAL_N", "40"))
ALLOWED = list(CATEGORY_TO_ACCOUNT.keys()) + ["Uncategorized"]
S = get_settings()

# (model, endpoint, api_key) for each contestant.
QWEN = (S.categorizer_model, S.categorizer_endpoint_url, S.categorizer_api_key)
FRONTIER = (
    os.getenv("EVAL_FRONTIER_MODEL", "accounting-ops-sonnet"),
    S.llm_gateway_base_url.rstrip("/") + "/v1",
    S.llm_platform_virtual_key,
)


async def categorize(client, model, endpoint, key, desc, amount):
    """One categorization call with the production prompt → (category, confidence)."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        *_FEW_SHOT_EXAMPLES,
        {"role": "user", "content": _build_user_message(
            payee=None, description=desc, memo=None,
            amount=Decimal(str(amount)), allowed_categories=ALLOWED)},
    ]
    body = {"model": model, "messages": messages, "max_tokens": 200, "temperature": 0.0}
    if "qwen" in model.lower():
        body["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        r = await client.post(endpoint.rstrip("/") + "/chat/completions",
                              json=body, headers=headers, timeout=90)
        r.raise_for_status()
        parsed = _parse_and_validate(_extract_completion(r.json()) or "", ALLOWED)
        return (parsed[0], parsed[1]) if parsed else ("Uncategorized", 0.0)
    except Exception as e:  # noqa: BLE001
        return (f"ERR:{type(e).__name__}", 0.0)


async def main():
    async with workspace_session(SessionLocal, WS) as s:
        rows = (await s.execute(text(
            "SELECT DISTINCT ON (left(payload->>'description',60)) "
            "(payload->>'amount_cents')::int AS cents, payload->>'description' AS d "
            "FROM approval_queue WHERE status='pending' "
            "AND subject_type='bank_transaction_import' "
            "AND COALESCE(payload->>'category','Uncategorized')='Uncategorized' "
            "AND payload->>'description' IS NOT NULL "
            "ORDER BY left(payload->>'description',60), cents LIMIT :n"
        ), {"n": N})).mappings().all()
    txns = [(r["d"], (r["cents"] or 0) / 100.0) for r in rows]

    qsem, fsem = asyncio.Semaphore(3), asyncio.Semaphore(8)

    async def one(client, desc, amt):
        async with qsem:
            q = await categorize(client, *QWEN, desc, amt)
        async with fsem:
            f = await categorize(client, *FRONTIER, desc, amt)
        return desc, amt, q, f

    t0 = time.monotonic()
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[one(client, d, a) for d, a in txns])
    dur = time.monotonic() - t0

    agree = both_cat = q_unc = f_unc = 0
    disagreements = []
    for desc, amt, (qc, qconf), (fc, fconf) in results:
        if qc == fc:
            agree += 1
        else:
            disagreements.append((amt, desc[:50], qc, qconf, fc, fconf))
        q_unc += qc == "Uncategorized"
        f_unc += fc == "Uncategorized"
        both_cat += qc != "Uncategorized" and fc != "Uncategorized"

    n = len(results) or 1
    print(f"\n===== CATEGORIZER EVAL: Qwen vs Frontier ({len(results)} txns, {dur:.0f}s) =====")
    print(f"Qwen={QWEN[0]}  Frontier={FRONTIER[0]}")
    print(f"AGREEMENT: {agree}/{len(results)} ({100*agree/n:.0f}%)")
    print(f"  Qwen Uncategorized: {q_unc} | Frontier Uncategorized: {f_unc} | both categorized: {both_cat}")
    print(f"\nDISAGREEMENTS ({len(disagreements)}):")
    for amt, desc, qc, qconf, fc, fconf in disagreements:
        print(f"  ${amt:>9.2f} {desc:50}\n      Qwen={qc!r}({qconf})  Frontier={fc!r}({fconf})")
    print("\nJSON_DISAGREEMENTS=" + json.dumps([
        {"amount": a, "desc": d, "qwen": qc, "qconf": qc2, "frontier": fc, "fconf": fc2}
        for a, d, qc, qc2, fc, fc2 in disagreements]))


if __name__ == "__main__":
    asyncio.run(main())

"""Qwen-vs-frontier evaluation harness for the CPA Narrative Generator (agent #3).

The decider for "can the local Qwen narrate a CPA review memo as faithfully as a
frontier model?" — the narrative agent's specific job is to SYNTHESISE a memo from
tax data it reads via tools; it must NEVER compute or invent a figure (engines are
the truth substrate). So the eval bar is FAITHFULNESS / NO-FABRICATION, not
classification-agreement.

Isolated-narration design (separates narration quality from tool-calling):
  1. Source = get_form_1120_data (exact, via MCP) + the tax_review_queue items
     (from the DB, prior cpa_narrative_* memos EXCLUDED — they are this agent's own
     past outputs, not source — and each item content truncated so the bundle fits
     a sane budget). Identical bundle fed to BOTH models → fair comparison.
  2. Feed it single-shot with the real system_prompt.md, correct params
     (no-think for qwen, temp 0.1, 16K out).
  3. Capture both memos + the source to EVAL_OUTDIR.
  4. Mechanical faithfulness screen: every $-figure in the memo must trace to a
     number present in the source bundle; unmatched = candidate fabrication.

A blind judge panel (run separately) scores faithfulness/completeness/quality.
Read-only: writes nothing to the books.

MODEL NOTE: the right Qwen for THIS agent is the 131K-context text endpoint
`qwen3.6-35b-p40` — NOT the categorizer's 8K `qwen3.6-35b-a3b` (a memo's OUTPUT
alone exceeds 8K). Pass EVAL_QWEN_KEY = a gateway key that can reach it.

Run inside the app container:
    MK=$(ssh magicunicorn 'docker exec unicorn-litellm printenv LITELLM_MASTER_KEY')
    cat scripts/eval_narrative.py | ssh midboy1 "docker exec -i \
      -e EVAL_YEAR=2023 -e EVAL_QWEN_KEY=$MK accounting_ops-app-1 python -"

Env overrides:
    EVAL_WORKSPACE_ID / EVAL_ENTITY_ID / EVAL_YEAR    which entity-year
    EVAL_QWEN_MODEL (default qwen3.6-35b-p40) / EVAL_QWEN_ENDPOINT / EVAL_QWEN_KEY
    EVAL_FRONTIER_MODEL (default accounting-ops-sonnet)
    EVAL_ITEM_TRUNC  per-item content char cap (default 2500)
    EVAL_OUTDIR      where to write memos + source (default /tmp/narrative_eval)
"""
import asyncio
import json
import os
import re
import time
from uuid import UUID

import httpx
from sqlalchemy import text as sqltext

from app.config import get_settings
from app.db import SessionLocal
from app.workspace_session import workspace_session

WS = os.getenv("EVAL_WORKSPACE_ID", "77753ea5-efe9-5539-a846-6b20ddc841b9")
ENT = os.getenv("EVAL_ENTITY_ID", "1ab938df-43f2-567a-8566-800f8a14a466")
YEAR = int(os.getenv("EVAL_YEAR", "2023"))
MCP_URL = os.getenv("ACCTOPS_MCP_URL", "http://localhost:8000/mcp")
TOKEN = os.environ["ACCTOPS_SERVICE_TOKEN"]
OUTDIR = os.getenv("EVAL_OUTDIR", "/tmp/narrative_eval")
ITEM_TRUNC = int(os.getenv("EVAL_ITEM_TRUNC", "2500"))
SYSTEM_PROMPT_PATH = "/app/agents/cpa_team/narrative_generator/system_prompt.md"

S = get_settings()
QWEN = (
    os.getenv("EVAL_QWEN_MODEL", "qwen3.6-35b-p40"),
    os.getenv("EVAL_QWEN_ENDPOINT", S.categorizer_endpoint_url),
    os.getenv("EVAL_QWEN_KEY", S.categorizer_api_key),
)
# Frontier = the narrative agent's REAL production default: OpenRouter sonnet-4.6
# (the cloud gateway llm.unicorncommander.ai has a ~60s proxy cap that 504s on a
# multi-page generation; OpenRouter is what trigger_tax_narrative actually uses).
FRONTIER = (
    os.getenv("EVAL_FRONTIER_MODEL", "anthropic/claude-sonnet-4.6"),
    os.getenv("EVAL_FRONTIER_ENDPOINT", "https://openrouter.ai/api/v1"),
    os.getenv("EVAL_FRONTIER_KEY") or os.environ.get("OPENROUTER_API_KEY") or S.llm_platform_virtual_key,
)


async def fetch_1120():
    import mcp.client.streamable_http as _t
    from mcp.client.session import ClientSession
    transport = _t.streamablehttp_client(url=MCP_URL, headers={"Authorization": f"Bearer {TOKEN}"})
    async with transport as (read, write, _):
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            r = await sess.call_tool("get_form_1120_data",
                                     {"workspace_id": WS, "entity_id": ENT, "tax_year": YEAR})
            return "\n".join(getattr(c, "text", None) or str(c) for c in (r.content or []))


async def fetch_queue_db():
    async with workspace_session(SessionLocal, UUID(WS)) as s:
        rows = (await s.execute(sqltext(
            "SELECT id::text AS item_id, item_type, status, "
            "COALESCE(form_reference,'') AS form_reference, content->>'topic' AS topic, "
            "left(content::text, :trunc) AS content "
            "FROM tax_review_queue "
            "WHERE entity_id=:e AND tax_year=:y "
            "AND COALESCE(content->>'topic','') NOT LIKE 'cpa_narrative%' "
            "ORDER BY (status='pending') DESC, status, item_type, form_reference"
        ), {"e": ENT, "y": YEAR, "trunc": ITEM_TRUNC})).mappings().all()
    return [dict(r) for r in rows]


def queue_to_text(items):
    out = [f"tax_review_queue: {len(items)} items "
           f"(prior cpa_narrative_* memos excluded; each content truncated to {ITEM_TRUNC} chars)"]
    for i, it in enumerate(items, 1):
        out.append(
            f"\n--- item {i} [id={it['item_id']}] "
            f"type={it['item_type']} status={it['status']} "
            f"form_reference={it['form_reference']} topic={it['topic']} ---\n{it['content']}"
        )
    return "\n".join(out)


def build_messages(system_prompt, f1120, queue_text):
    user = (
        "Please generate the CPA Narrative Memo for the following entity-year.\n\n"
        f"workspace_id : {WS}\nentity_id    : {ENT}\ntax_year     : {YEAR}\n"
        "run_date     : 2026-06-13\nrequesting_cpa : Morgan Qureshi\n\n"
        "The data you would normally fetch via get_form_1120_data and "
        "get_tax_review_queue has ALREADY been retrieved and is provided verbatim "
        "below. Do NOT call any tools. Compose the full 11-section Markdown narrative "
        "directly from this data. Use ONLY figures and IDs that appear in this data; "
        "do NOT invent, estimate, or compute new dollar amounts. Begin directly with "
        "the '# CPA Review Memo' H1 (no preamble).\n\n"
        f"=== get_form_1120_data ===\n{f1120}\n\n"
        f"=== get_tax_review_queue (include_resolved=true) ===\n{queue_text}\n"
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}]


async def generate(client, model, endpoint, key, messages):
    body = {"model": model, "messages": messages, "max_tokens": 16384, "temperature": 0.1}
    if "qwen" in model.lower():
        body["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    t0 = time.monotonic()
    r = await client.post(endpoint.rstrip("/") + "/chat/completions",
                          json=body, headers=headers, timeout=900)
    r.raise_for_status()
    j = r.json()
    return (j["choices"][0]["message"].get("content") or ""), time.monotonic() - t0, j.get("usage", {})


_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_MONEY = re.compile(r"\$\s?(-?[\d,]+(?:\.\d+)?)")


def source_numbers(*blobs):
    s = set()
    for b in blobs:
        for tok in _NUM.findall(b):
            try:
                v = float(tok.replace(",", ""))
            except ValueError:
                continue
            s.add(round(v))
            s.add(round(v / 100))
    return s


def screen(memo, src):
    figs = []
    for m in _MONEY.finditer(memo):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        ctx = memo[max(0, m.start() - 45):m.start()].replace("\n", " ")
        figs.append((m.group(0), round(v), ctx))
    unsourced = [f for f in figs if f[1] not in src and round(f[1] * 100) not in src and f[1] != 0]
    return figs, unsourced


async def main():
    os.makedirs(OUTDIR, exist_ok=True)
    run_set = set(os.getenv("EVAL_RUN", "qwen,frontier").split(","))
    with open(SYSTEM_PROMPT_PATH) as fh:
        system_prompt = fh.read()
    print(f"[eval] entity={ENT} year={YEAR}  qwen={QWEN[0]}  frontier={FRONTIER[0]}  run={sorted(run_set)}")
    p1, pq = f"{OUTDIR}/source_1120.json", f"{OUTDIR}/source_queue.txt"
    if os.getenv("EVAL_REUSE_SOURCE") == "1" and os.path.exists(p1) and os.path.exists(pq):
        with open(p1) as fh:
            f1120 = fh.read()
        with open(pq) as fh:
            queue_text = fh.read()
        print("[eval] reusing saved source bundle (byte-identical input for both models)")
    else:
        print("[eval] fetching 1120 (MCP) + queue (DB) ...")
        f1120 = await fetch_1120()
        queue_text = queue_to_text(await fetch_queue_db())
        with open(p1, "w") as fh:
            fh.write(f1120)
        with open(pq, "w") as fh:
            fh.write(queue_text)
    approx = (len(f1120) + len(queue_text)) // 4
    print(f"[eval]   1120={len(f1120)}b  queue={len(queue_text)}b  ~input_tokens≈{approx}")
    messages = build_messages(system_prompt, f1120, queue_text)
    src = source_numbers(f1120, queue_text)
    print(f"[eval]   source distinct numbers: {len(src)}")

    summary = {}
    async with httpx.AsyncClient() as client:
        for label, (model, ep, key) in [("qwen", QWEN), ("frontier", FRONTIER)]:
            if label not in run_set:
                continue
            print(f"\n[eval] generating with {label} ({model}) ...")
            try:
                memo, dur, usage = await generate(client, model, ep, key, messages)
            except Exception as e:  # noqa: BLE001
                print(f"[eval]   {label} ERROR: {type(e).__name__}: {str(e)[:400]}")
                summary[label] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
                continue
            if not memo.strip():
                print(f"[eval]   {label} EMPTY (usage={usage})")
                summary[label] = {"error": "empty", "usage": usage}
                continue
            with open(f"{OUTDIR}/memo_{label}.md", "w") as fh:
                fh.write(memo)
            figs, unsourced = screen(memo, src)
            secs = len([ln for ln in memo.splitlines() if ln.startswith("## ")])
            print(f"[eval]   {label}: {dur:.0f}s chars={len(memo)} words={len(memo.split())} "
                  f"##sections={secs} $figs={len(figs)} UNSOURCED={len(unsourced)} usage={usage}")
            for token, val, ctx in unsourced[:30]:
                print(f"            {token:>14}   …{ctx[-44:]}")
            summary[label] = {"dur_s": round(dur), "chars": len(memo), "words": len(memo.split()),
                              "sections": secs, "figures": len(figs), "unsourced": len(unsourced),
                              "unsourced_samples": [(t, v) for t, v, _ in unsourced[:30]]}

    print("\n[eval] saved to", OUTDIR)
    print("JSON_SUMMARY=" + json.dumps(summary))


if __name__ == "__main__":
    asyncio.run(main())

"""Qwen-vs-frontier evaluation harness for the CHAT PERSONAS (agents #4..#16).

Per Aaron's "measure don't guess" mandate: for each persona, run a representative
domain question that tests THAT persona's specific competence, on both the local
131K Qwen (qwen3.6-35b-p40 — the right model; the 8K categorizer model can't even
hold several persona system prompts) and the frontier model it would replace.
Single-shot (no tool loop) → isolates DOMAIN REASONING quality, which is the
primary signal: if Qwen can't reason in the domain, tool-use is moot.

Spanning subset across the reasoning-depth spectrum (locate where the line falls):
  SIMPLE   : tax_research (RAG/cite), setup_assistant (onboarding)
  MODERATE : tax_resolver (rule application), 1099_specialist (reporting judgment)
  DEEP     : cfo_agent (strategic synthesis), federal_strategy_drafter (§382 math+law)

Read-only. Saves each answer to EVAL_OUTDIR for a blind judge panel.

Run inside the app container:
    MK=$(ssh magicunicorn 'docker exec unicorn-litellm printenv LITELLM_MASTER_KEY')
    cat scripts/eval_personas.py | ssh midboy1 "docker exec -i \
      -e EVAL_QWEN_KEY=$MK accounting_ops-app-1 python -"

Env overrides:
    EVAL_PERSONAS   comma list of labels to run (default = the spanning 6)
    EVAL_QWEN_MODEL (default qwen3.6-35b-p40) / EVAL_QWEN_KEY
    EVAL_FRONTIER_MODEL (default anthropic/claude-sonnet-4.6) via OpenRouter
    EVAL_OUTDIR     (default /tmp/persona_eval)
"""
import asyncio
import json
import os
import time

import httpx

from app.config import get_settings

S = get_settings()
OUTDIR = os.getenv("EVAL_OUTDIR", "/tmp/persona_eval")
QWEN = (
    os.getenv("EVAL_QWEN_MODEL", "qwen3.6-35b-p40"),
    os.getenv("EVAL_QWEN_ENDPOINT", S.categorizer_endpoint_url),
    os.getenv("EVAL_QWEN_KEY", S.categorizer_api_key),
)
FRONTIER = (
    os.getenv("EVAL_FRONTIER_MODEL", "anthropic/claude-sonnet-4.6"),
    os.getenv("EVAL_FRONTIER_ENDPOINT", "https://openrouter.ai/api/v1"),
    os.getenv("EVAL_FRONTIER_KEY") or os.environ.get("OPENROUTER_API_KEY") or "",
)

_AG = "/app/agents"
# label -> (system_prompt_path, depth, question)
PERSONAS = {
    "tax_research": (
        f"{_AG}/tax_research/conversational_researcher/system_prompt.md", "SIMPLE",
        "What does IRC §174 require for software development costs in tax year 2023, "
        "and did the OBBBA (enacted 2025) change it? Cite the controlling authority. "
        "Do NOT advise on any specific entity's situation — just state the law."),
    "setup_assistant": (
        f"{_AG}/cpa_team/setup_assistant/system_prompt.md", "SIMPLE",
        "I'm setting up books for my single-member SC business, a C-corp. I also do "
        "freelance design work under a DBA called 'Fabrikam Design.' Should that DBA be a "
        "separate entity or a sub-account within the corp, and what chart-of-accounts "
        "categories do I need to get started?"),
    "tax_resolver": (
        f"{_AG}/cpa_team/tax_resolver/system_prompt.md", "MODERATE",
        "A bank transaction — $4,200 to 'Stripe' — was categorized with confidence 0.45. "
        "Our escalation rule: escalate to CPA if confidence < 0.5 AND amount > $5,000; "
        "otherwise auto-resolve to the suggested category. Does this transaction escalate "
        "or auto-resolve? Walk through the rule application and give the disposition."),
    "1099_specialist": (
        f"{_AG}/tax_specialists/1099_specialist/system_prompt.md", "MODERATE",
        "In 2023 we paid: $1,200 to a sole-proprietor contractor, $800 to a law firm (LLC) "
        "for legal services, and $5,000 to an S-corp consultant — all via Stripe. Which "
        "payments require a 1099-NEC, which are excepted, and how does paying through Stripe "
        "change the analysis? Be specific about the attorney rule and the payment-card exception."),
    "cfo_agent": (
        f"{_AG}/cpa_team/cfo_agent/system_prompt.md", "DEEP",
        "Our pre-revenue SC C-corp burned $3,861 in TY2023 against $19.60 of revenue, funded "
        "entirely by owner contributions. We're weighing a $250K SAFE round in 2026 and whether "
        "to convert to an LLC first. Give me your strategic read: cash-runway framing, the §382 "
        "NOL-preservation risk the raise creates, and your recommendation on entity structure. "
        "Be decisive."),
    "federal_strategy_drafter": (
        f"{_AG}/tax_specialists/federal_strategy_drafter/system_prompt.md", "DEEP",
        "A C-corp has a $400,000 federal NOL carryforward. It's raising a Series A that moves "
        "new 5% investors from 30% to 70% ownership. Assume equity value of $2,000,000 and a "
        "§382 long-term tax-exempt rate of 3.5%. Determine whether this triggers a §382 "
        "ownership change, compute the annual §382 limitation if it applies, and recommend an "
        "NOL-preservation strategy. Show the computation."),
}


async def generate(client, model, endpoint, key, system_prompt, question):
    body = {"model": model,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": question}],
            "max_tokens": 4096, "temperature": 0.2}
    if "qwen" in model.lower():
        body["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    t0 = time.monotonic()
    r = await client.post(endpoint.rstrip("/") + "/chat/completions",
                          json=body, headers=headers, timeout=600)
    r.raise_for_status()
    j = r.json()
    msg = j["choices"][0]["message"]
    return (msg.get("content") or ""), time.monotonic() - t0, j.get("usage", {})


async def main():
    os.makedirs(OUTDIR, exist_ok=True)
    want = os.getenv("EVAL_PERSONAS")
    labels = want.split(",") if want else list(PERSONAS.keys())
    print(f"[eval] personas={labels}\n[eval] qwen={QWEN[0]}  frontier={FRONTIER[0]}")
    summary = {}
    async with httpx.AsyncClient() as client:
        for label in labels:
            sppath, depth, question = PERSONAS[label]
            with open(sppath) as fh:
                sysp = fh.read()
            sp_tok = len(sysp) // 4
            print(f"\n[eval] === {label} ({depth}) sys≈{sp_tok}tok ===")
            row = {"depth": depth, "sys_tokens": sp_tok}
            for who, (model, ep, key) in [("qwen", QWEN), ("frontier", FRONTIER)]:
                try:
                    ans, dur, usage = await generate(client, model, ep, key, sysp, question)
                except Exception as e:  # noqa: BLE001
                    print(f"[eval]   {who} ERROR: {type(e).__name__}: {str(e)[:300]}")
                    row[who] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
                    continue
                if not ans.strip():
                    print(f"[eval]   {who} EMPTY (usage={usage})")
                    row[who] = {"error": "empty", "usage": usage}
                    continue
                path = f"{OUTDIR}/{label}__{who}.md"
                with open(path, "w") as fh:
                    fh.write(f"# Persona: {label} ({depth})\n## Question\n{question}\n\n## Answer ({who})\n{ans}")
                ct = usage.get("completion_tokens", "?")
                print(f"[eval]   {who:8} {dur:.0f}s words={len(ans.split())} out_tok={ct}")
                row[who] = {"path": path, "dur_s": round(dur), "words": len(ans.split()), "out_tok": ct}
            summary[label] = row
    with open(f"{OUTDIR}/summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    print("\n[eval] saved to", OUTDIR)
    print("JSON_SUMMARY=" + json.dumps(summary))


if __name__ == "__main__":
    asyncio.run(main())

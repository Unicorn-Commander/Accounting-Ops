# CFO Agent

Strategic-finance synthesis layer for Acme's Accounting-Ops. Lives in
the `/chat` surface as a top-tier persona, and federates to Brigade as a
sub-agent when Brigade is online.

**the owner, the admin — this agent is your CFO sounding board for cash, runway,
unit economics, financing, and board narrative. It is NOT a licensed CFO,
CPA, attorney, or 409A provider. It frames; you decide.**

---

## Purpose

The CFO Agent answers questions like:

- "What's our cash runway under the current burn?"
- "If we close a $500K seed at $4M post, what's the dilution?"
- "Which year-end strategy moves materially affect TY2026 federal tax?"
- "Draft the board-deck financials slide for Q3."
- "How does Acme's gross margin compare to Sample LLC'?"

It does this by reading the same data the operational + specialist agents
expose (P&L, runway, form-1120 working data, tax rules) and synthesizing it
into a board-room view. It NEVER replicates operational or specialist work
— for that it surfaces to the right agent / human.

---

## Tools

| Tool | Scope | Purpose |
|---|---|---|
| `get_p_and_l` | local | P&L for an entity, any period token |
| `get_consolidated_p_and_l` | local | Workspace-wide consolidated P&L |
| `get_runway` | local | Months-of-cash projection |
| `get_period_journal` | local | Journal entries for a window (read-only) |
| `list_transactions` / `get_transaction` | local | Drill-down on individual entries |
| `get_form_1120_data` | local | Form 1120 working data |
| `get_1099_payees` | local | Contractor concentration |
| `get_current_rule` | local | Tax-rule lookup (RAG over the curated KB) |
| `query_specialty_rules` | local | Specialty-rule lookup |
| `draft_memo` | local | Queue a CFO framing memo (audit trail) |
| `flag_for_cpa_review` | local | Surface to CPA Tax Resolver |
| `project.*` | brigade | Project-Ops budgets / burn (when federated) |
| `meeting.*` | brigade | Meeting-Ops decisions log (when federated) |
| `contact.*` | brigade | Customer revenue concentration (when federated) |

Tools NOT in the allow-list:
`categorize_transaction`, `propose_payment`, `link_receipt_to_transaction`,
`add_specialty_rule`. Operational mutations stay in the operational layer.

---

## Scope — what the CFO Agent does NOT do

1. **No tax positions.** Hands off to CPA Tax Resolver.
2. **No legal sign-off.** Surfaces to attorney.
3. **No 409A.** Surfaces to a 409A provider.
4. **No securities advice / treasury management.** Surfaces to advisor.
5. **No journal mutations.** Drafts memos only. Operational layer commits.
6. **No cross-app reads without `brigade:cross_app` scope.**
7. **No PII across apps without explicit consent.**

When a question crosses any of these, the agent responds:

> "I'll surface this to your [role] — here's the framing I've prepared:"

…and emits a `draft_memo` framing memo with topic `cfo_surface_*`.

---

## Model

Default: **Claude Opus 4.7** (higher reasoning depth on strategic synthesis).
Fallback: **Claude Sonnet 4.5** (if Opus unreachable).
Override: `CFO_AGENT_MODEL` env var.

---

## Brigade federation

This agent is both PRODUCER and CONSUMER of Brigade tools:

- **Producer**: Brigade ingests `/.well-known/mcps.json` from Accounting-Ops
  and exposes the CFO Agent's synthesis tools (`account.*`) to other Brigade
  clients.
- **Consumer**: when Brigade is live at `https://brigade.example.com`,
  the agent loads the aggregated cross-app catalog from
  `/.well-known/manifest.json` and the in-app `/chat` can call `project.*`,
  `meeting.*`, `contact.*` tools.

When Brigade is offline (the default until commander wires it up), the
consumer side degrades gracefully — `account.*` works, cross-app is empty,
log line: `Brigade unavailable — local tools only`.

See `agents/_shared/brigade_federation.md` for the full federation spec
(producer side) and `agents/_shared/cross_app_strategy.md` for when the CFO
Agent reaches for cross-app tools.

---

## Files

```
agents/cpa_team/cfo_agent/
├── README.md                    ← you are here
├── persona.md                   ← Brigade-compatible persona + guardrails
├── system_prompt.md             ← LLM system turn (verbatim)
└── __init__.py
```

---

## Local invocation (from `/chat`)

The agent is registered in `agents/chat/persona_registry.py` as `cfo-agent`.
Selecting it from the chat persona dropdown sets:

- `model` = Claude Opus 4.7 (or `CFO_AGENT_MODEL` override).
- `tools` = the allow-list above (the read + draft set, plus `brigade:*`
  catalog when federated).
- `system_prompt` = `system_prompt.md` (loaded at server start).
- `needs_tax_year` = `true` (chat prompts the user to pick TY before any
  tax-flavored question — e.g. year-end strategy).

---

## Why this agent exists

Up to now, the agents have been operational (Setup Assistant, Sentinel,
Receipt Reconciler) or specialist (Form 1120 Preparer, SC State, etc.).
Both are tactical: they DO things.

The CFO Agent is the synthesis layer — it doesn't DO, it THINKS. When the owner
asks "should we raise now or push to Q4?", that question doesn't belong in
any operational or specialist agent. It belongs at the CFO seat.

This agent fills that seat. It will never replace a human CFO, but it
removes 80% of the "let me think through this on paper" overhead so the
human CFO (the owner) can spend more time on the 20% that actually requires
human judgment, relationships, and accountability.

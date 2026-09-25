# Tax Specialists

Agent-based tax-preparation specialists for Accounting-Ops. Each specialist is a single-
purpose LLM-driven agent that reads from the platform's MCP tools, produces a DRAFT
output, and writes structured memos and CPA flags to the `tax_review_queue`.

**None of these specialists are CPAs. All output is a DRAFT for licensed CPA review.**

---

## What's here

```
agents/tax_specialists/
├── README.md                          ← you are here
├── coordinator/                       ← Tax Operations Coordinator (orchestrator)
│   ├── persona.md                     ← role, guardrails ("dispatch and aggregate only")
│   ├── system_prompt.md               ← verbatim system prompt for the coordinator LLM
│   ├── dispatch_plan.py               ← deterministic rule-based dispatcher (no LLM)
│   └── run_coordinator.py             ← CLI runner — launches specialists as subprocesses
├── form_1120_preparer/
│   ├── persona.md                     ← role, ethics, guardrails document
│   ├── system_prompt.md               ← verbatim system prompt sent to the LLM
│   ├── run_specialist.py              ← CLI runner (Anthropic SDK + MCP)
│   └── worked_examples/
│       ├── 2023_pre_revenue_c_corp.md
│       ├── 2024_revenue_with_contractor_payments.md
│       └── 2025_inter_entity_transfer.md
└── sc_state_specialist/
    ├── persona.md                     ← role, SC scope, 7 guardrails
    ├── system_prompt.md               ← verbatim system prompt (SC1120, failure-to-file, use tax)
    ├── run_specialist.py              ← CLI runner (Anthropic SDK + MCP; SC _ALLOWED_TOOLS)
    └── worked_examples/
        ├── mu_inc_2023_failure_to_file_cure.md
        ├── 2024_normal_sc1120_with_federal_modifications.md
        └── 2024_sales_use_tax_sweep.md
```

---

## Specialist inventory

| Specialist | File | Status | Coverage |
|---|---|---|---|
| **Tax Operations Coordinator** | `coordinator/` | **MVP ready** | Orchestrator — dispatches requests to specialists + aggregates outputs |
| Form 1120 Preparer | `form_1120_preparer/` | MVP ready | Federal C-corp return, Schedule L, 1099-NEC obligations |
| 1099 Specialist | *(not yet built)* | Planned | Detailed 1099-NEC / 1099-MISC filing package per entity |
| SC State Tax Specialist | `sc_state_specialist/` | MVP ready | SC1120 C-corp return, Schedule B modifications, failure-to-file cure, sales/use tax sweep |
| Payroll Advisor | *(not yet built)* | Planned | W-2 / 941 gap analysis and filing calendar |
| Audit Defense Specialist | *(not yet built)* | Planned | Response to IRS / SCDOR notices; depends on SC State output |

---

## Running from the web app ("Run drafts")

Accountants (and above) on a C-corporation entity can start the coordinator from
the tax page (`/taxes/<year>` → **Run drafts**). The confirm dialog shows the
model, the provider (and whether it is a third-party API) and what data is sent.
The backend (`app/api/tax_run_launch.py`) queues a `specialist_run` row
(`status='queued'`, `agent_token_id='app-launcher'`), runs
`run_coordinator.py --backend openai` in the app process with the app's own
`ACCTOPS_SERVICE_TOKEN` + `CPATEAM_LLM_*` route, writes outputs to
`<DRAFTS_DIR>/<workspace>/<entity>/runs/<run_id>/`, and on success copies them
to the canonical `<year>-<slug>-draft.md` names the draft viewer reads (older
canonical drafts are kept as `*.superseded-<run8>.md`). One active run per
entity + year; cancel from Run history. Off unless `AO_TAX_RUN_LAUNCH_ENABLED=1`.

## Running the Coordinator (recommended entry point)

The Coordinator is the normal way to prepare a full tax package. It dispatches
to the right specialists automatically based on your request.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export ACCTOPS_SERVICE_TOKEN="<keycloak-service-token>"

# Full federal + 1099 + SC trio for Acme Inc 2023:
uv run python agents/tax_specialists/coordinator/run_coordinator.py \
    --workspace-id 550e8400-e29b-41d4-a716-446655440000 \
    --entity-id    aabbccdd-1234-5678-9abc-000000000001 \
    --tax-year     2023 \
    --request      "prepare 2023 returns for Acme Inc" \
    --output-dir   ./drafts/

# Dry run — print dispatch plan without launching any specialist:
uv run python agents/tax_specialists/coordinator/run_coordinator.py \
    --workspace-id 550e8400-e29b-41d4-a716-446655440000 \
    --entity-id    aabbccdd-1234-5678-9abc-000000000001 \
    --tax-year     2023 \
    --request      "prepare 2023 returns for Acme Inc" \
    --dry-run

# With a self-hosted LLM backend (e.g. vLLM on <host>):
uv run python agents/tax_specialists/coordinator/run_coordinator.py \
    --workspace-id 550e8400-e29b-41d4-a716-446655440000 \
    --entity-id    aabbccdd-1234-5678-9abc-000000000001 \
    --tax-year     2023 \
    --request      "prepare 2023 returns for Acme Inc" \
    --backend      openai \
    --llm-endpoint-url http://<host>:8088/v1 \
    --output-dir   ./drafts/
```

The coordinator will:
1. Classify your request and build a dispatch plan (deterministic — no LLM).
2. Sanity-check the entity has data via `get_form_1120_data`.
3. Launch 1120 Preparer + 1099 Specialist in parallel (group 1).
4. After group 1 completes, launch SC State Specialist (group 2).
5. Write a coordinator summary memo via `draft_memo`.
6. Save `./drafts/coordinator-{entity}-2023-{ts}.md` with all outputs linked.

Gap notice: 1099 Specialist and Audit Defense Specialist are not yet built. The
coordinator writes gap notices for them and completes the run with whichever specialists
are available. SC State Tax Specialist is now live and will be dispatched automatically.

---

## Running a specialist directly

### Form 1120 Preparer

```bash
# Prerequisites
export ANTHROPIC_API_KEY="sk-ant-..."
export ACCTOPS_SERVICE_TOKEN="<keycloak-service-token>"
# Optional: override the MCP endpoint (default: http://localhost:8000/mcp)
export ACCTOPS_MCP_URL="http://localhost:8000/mcp"

# Run (requires uv + the tax-agents extras installed)
uv run python agents/tax_specialists/form_1120_preparer/run_specialist.py \
    --workspace-id 550e8400-e29b-41d4-a716-446655440000 \
    --entity-id    aabbccdd-1234-5678-9abc-000000000001 \
    --tax-year     2023 \
    --output       ./drafts/mu-inc-2023-1120-draft.md \
    --model        sonnet
```

Use `--model opus` for higher-quality output on complex returns (inter-entity transfers,
multiple entities, large journal sets). Sonnet is the default for cost efficiency.

### SC State Tax Specialist

Run the Form 1120 Preparer **first** for the same entity + tax year. The SC specialist
reads the federal package via `get_form_1120_data` as its starting point.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export ACCTOPS_SERVICE_TOKEN="<keycloak-service-token>"

# Step 1: Run federal 1120 Preparer first
uv run python agents/tax_specialists/form_1120_preparer/run_specialist.py \
    --workspace-id 550e8400-e29b-41d4-a716-446655440000 \
    --entity-id    aabbccdd-1234-5678-9abc-000000000001 \
    --tax-year     2023 \
    --output       ./drafts/mu-inc-2023-1120-draft.md

# Step 2: Run SC State Tax Specialist
uv run python agents/tax_specialists/sc_state_specialist/run_specialist.py \
    --workspace-id 550e8400-e29b-41d4-a716-446655440000 \
    --entity-id    aabbccdd-1234-5678-9abc-000000000001 \
    --tax-year     2023 \
    --output       ./drafts/mu-inc-2023-sc1120-draft.md \
    --model        sonnet
```

The SC specialist will: (1) read the federal package; (2) detect failure-to-file and draft
the cure package; (3) apply SC modifications (bonus depreciation addback, SC NOL); (4) sweep
the journal for sales/use tax exposure; (5) write flags for all SC judgment calls.

### Getting a service token

```bash
# Exchange your Keycloak credentials for a token (uchub realm, accounting-ops-app client)
curl -s \
  -d "client_id=accounting-ops-app" \
  -d "client_secret=<secret>" \
  -d "grant_type=client_credentials" \
  "https://auth.example.com/realms/uchub/protocol/openid-connect/token" \
  | jq -r .access_token
```

---

## Standard guardrails for all specialists

Every specialist in this directory MUST adhere to the following non-negotiable rules.
These are enforced in each specialist's `persona.md` and `system_prompt.md`.

1. **DRAFT-only.** All output is stamped DRAFT — CPA REVIEW REQUIRED. No specialist
   produces final output.

2. **No signing or filing.** Specialists have no authority to sign, transmit, or file
   any tax document.

3. **Cite-as-you-go.** Every numeric position carries confidence, source citations
   (journal entry IDs, COA categories), and IRS §code / Publication references.

4. **Flag judgment calls.** Material ambiguities go to `flag_for_cpa_review` via MCP —
   never silently placed on a line.

5. **No tax advice.** Output is addressed to the CPA, not to the entity owner.

6. **Real citations only.** §codes and IRS Publications cited by number only. No
   invented references.

7. **Append-only audit trail.** All memos and flags written via `draft_memo` and
   `flag_for_cpa_review` MCP tools are immutable queue rows.

---

## How to add a new specialist

1. **Create the directory:**
   ```
   agents/tax_specialists/<specialist_name>/
   ```

2. **Write `persona.md`:** Role definition, scope, guardrails. Keep to 1–2 pages.
   Copy the guardrail structure from `form_1120_preparer/persona.md`; extend only
   what is specific to the new specialist's form/scope.

3. **Write `system_prompt.md`:** The verbatim LLM system prompt. Required sections:
   - Preamble (identity + hard guardrails distilled from persona.md)
   - Tools available + when to use each
   - Output schema (Markdown structure + required fields)
   - Category-specific treatments (the specialist-equivalent of the 4-flagged-category
     section in the 1120 preparer)
   - Refusal patterns

4. **Write worked examples** in `worked_examples/`: at minimum 2 examples covering
   a normal case and an edge/judgment case. Format: synthetic scenario → tool call
   sequence → expected output + key teaching points.

5. **Write `run_specialist.py`:** Copy the pattern from `form_1120_preparer/run_specialist.py`.
   Key customization points:
   - `_ALLOWED_TOOLS`: list only the MCP tools this specialist needs. Principle of
     least privilege — do not include write tools the specialist does not require.
   - `_build_user_message`: adjust the task framing for the new form/scope.
   - `_MODEL_MAP`: same map; pick the same defaults unless the new specialist's
     task has materially different complexity.

6. **Update this README:** Add the new specialist to the inventory table.

7. **Do not add HTTP routes or DB tables.** All backend tool additions are owned by the
   `app/` directory. This `agents/` tree is additive-only.

---

## Architecture note

Each specialist is a standalone stateless agent:

```
run_specialist.py
    │
    ├── [Anthropic SDK]  ──→  Claude (model)
    │                              │
    │                              ├── tool call: get_form_1120_data(...)
    │                              ├── tool call: get_1099_payees(...)
    │                              ├── tool call: draft_memo(...)
    │                              └── tool call: flag_for_cpa_review(...)
    │                              │
    │         [MCP HTTP transport] │
    │                              ▼
    └── [Accounting-Ops /mcp]  ──→  FastMCP server (mcp_server.py)
                                         │
                                         └── Postgres (journal, tax_review_queue)
```

The model drives its own tool-call loop via the Anthropic SDK's native MCP integration.
The runner script does not implement a tool-call loop — it delegates entirely to the SDK.

For a more complex multi-agent workflow (e.g., a CPA review agent that reads the
`tax_review_queue` and responds to flags), this same pattern applies: write a new
`run_specialist.py` that connects to the same MCP endpoint with a different system prompt
and a different `_ALLOWED_TOOLS` list.

---

## State tax specialist pattern (GA, NC, NY, etc.)

To add a state tax specialist for any state, use `sc_state_specialist/` as the template:

1. Replace SC-specific rules with the target state's equivalents:
   - State income tax rate and form name (e.g., GA Form 600, NC CD-405).
   - State IRC conformity date and decoupled provisions.
   - State NOL rules (carryforward years, carryback allowed?).
   - State apportionment formula (single-factor sales vs. three-factor).
   - State sales/use tax rate and nexus rules.
   - State failure-to-file penalty formula and cure procedure.
2. Set `_ALLOWED_TOOLS = ["get_form_1120_data", "get_period_journal", "draft_memo", "flag_for_cpa_review"]`.
3. The call sequence — `get_form_1120_data` first, then `get_period_journal` for detail — is
   invariant across all state specialists.
4. Prefix all `form_reference` values with the state form name (e.g., `"GA Form 600, ..."`)
   to distinguish from SC and federal flags in the shared `tax_review_queue`.

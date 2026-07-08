# CPA Team

Agent-based CPA review layer for Accounting-Ops. These agents consume the
`tax_review_queue` built by the `tax_specialists/` team and programmatically
resolve deterministic items, escalating only the fraction that requires true
human CPA judgment.

**None of these agents are CPAs. All output is DRAFT. A human CPA must review
all resolutions and escalations before any return is filed.**

---

## Directory Structure

```
agents/cpa_team/
├── README.md                       ← you are here
└── tax_resolver/
    ├── persona.md                  ← Brigade-compatible persona + guardrails
    ├── system_prompt.md            ← LLM system prompt (escalation second-opinions)
    ├── resolution_rules.py         ← deterministic rule engine (pure Python, no I/O)
    ├── run_cpa_resolver.py         ← CLI runner (Brigade-compatible, dual-backend)
    ├── tests_resolution_rules.py   ← unit tests for the rule engine
    └── worked_examples/
        ├── 2023_full_resolution_pass.md   ← Acme Inc 2023 pre-revenue full pass
        ├── escalation_examples.md         ← the 4 mandatory escalation categories
        └── 4_flagged_categories.md        ← Form 1120's 4 always-flagged categories
```

---

## Quick Start (Production — docker exec)

```bash
ssh user@<host>
TOKEN=$(cat /home/user/.tax-1120-preparer.token)
OPENROUTER_KEY=$(cat /home/user/.openrouter.key)

docker exec \
  -e ACCTOPS_SERVICE_TOKEN="$TOKEN" \
  accounting_ops-app-1 \
  python /app/agents/cpa_team/tax_resolver/run_cpa_resolver.py \
    --workspace-id 00000000-0000-0000-0000-000000000000 \
    --entity-id    11111111-1111-1111-1111-111111111111 \
    --tax-year     2023 \
    --acctops-base-url http://localhost:8000 \
    --output       /drafts/cpa-resolution-report-2023.md \
    --backend      openai \
    --llm-endpoint-url https://openrouter.ai/api/v1 \
    --llm-model    anthropic/claude-sonnet-4-6 \
    --llm-api-key  "$OPENROUTER_KEY" \
    --verbose
```

Repeat with `--tax-year 2022`, `2024`, `2025` for the full backlog.

---

## Quick Start (Development — dry run)

```bash
export ACCTOPS_SERVICE_TOKEN="..."
uv run python agents/cpa_team/tax_resolver/run_cpa_resolver.py \
    --workspace-id <UUID> \
    --entity-id    <UUID> \
    --tax-year     2023 \
    --output       ./drafts/dry-run-2023.md \
    --dry-run \
    --verbose
```

`--dry-run` fetches and classifies items but does NOT call POST /resolve.
Use this to preview what the resolver would do before committing.

---

## Running the Unit Tests

```bash
# From the repo root (uv environment):
uv run python -m pytest agents/cpa_team/tax_resolver/tests_resolution_rules.py -v

# Or as a plain script (works inside the container):
python /app/agents/cpa_team/tax_resolver/tests_resolution_rules.py
```

All 43 tests should pass with no DB or network required.

---

## Service Token

The `tax-1120-preparer` service token (Keycloak, uchub realm, `role_floor=accountant`)
is sufficient for all resolver operations. It can call:
- `GET /api/workspaces/{wid}/tax-review-queue` (viewer — included in accountant)
- `POST /api/workspaces/{wid}/tax-review-queue/{id}/resolve` (accountant)

No new Keycloak service account is needed.

---

## Resolution Rule Coverage

The rule engine handles all item types produced by the specialist team:

| Item type | Rules | Coverage |
|---|---|---|
| `form_line_proposal` | FLP-HIGH, FLP-MED (→ resolved); FLP-LOW (→ escalate) | 100% |
| `flagged_position` | 15 category-specific rules + 3 catch-all rules | ~95%+ |
| `memo` | MEMO-DELIVERABLE, MEMO-RESOLVER, MEMO-GENERIC | 100% |
| `question` | Q-1125E, Q-941 (→ resolved); Q-ESCALATE (fallback) | ~40% deterministic |

For a typical pre-revenue entity year (e.g., Acme Inc 2023), ~100% of items are handled
deterministically. For revenue years with inter-entity activity or officer compensation,
expect 10-20% escalation rate.

---

## Mandatory Escalations (Hardcoded)

These are NEVER auto-resolved, regardless of LLM second-opinion:

| Rule | Category |
|---|---|
| `FP-382` | §382 ownership-change |
| `FP-INTER-ENTITY` | Inter-entity transfer |
| `FP-1099-OFFICER` | Officer 1099-NEC |
| `FP-SOS-BAD` | SC SOS bad/unknown standing |
| `FP-941-EXISTS` | Form 941 with payroll detected |
| `FLP-LOW` | Low-confidence form_line_proposal |
| `FP-OTHER-LOW` | Low-confidence unclassified flag |
| `Q-ESCALATE` | Open question |

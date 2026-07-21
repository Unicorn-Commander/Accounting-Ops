# Form 1120 Preparer — Specialist README

AI-driven DRAFT Form 1120 (C-corporation federal income tax return) preparer.
All output is stamped **DRAFT — CPA REVIEW REQUIRED** and is intended for
review by a licensed CPA. The specialist is not a CPA, enrolled agent, or
tax preparer and cannot sign, transmit, or file any return.

---

## Quick start

### Prerequisites

```bash
# Shared requirement for both backends
export ACCTOPS_SERVICE_TOKEN="<keycloak-service-token>"
# See "Getting a service token" below for how to obtain this.

# Optional: override the MCP endpoint (default: http://localhost:8000/mcp)
export ACCTOPS_MCP_URL="http://localhost:8000/mcp"
```

---

### Backend 1 — Anthropic API (Claude Sonnet / Opus)

Best for: production runs, highest reasoning quality, complex returns with
inter-entity transfers or low-confidence lines.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."

uv run python agents/tax_specialists/form_1120_preparer/run_specialist.py \
    --backend      anthropic \
    --workspace-id 550e8400-e29b-41d4-a716-446655440000 \
    --entity-id    aabbccdd-1234-5678-9abc-000000000001 \
    --tax-year     2023 \
    --output       ./drafts/mu-inc-2023-1120-draft.md \
    --model        sonnet
```

Use `--model opus` for higher-quality output (higher cost) on returns with
inter-entity transfers, multiple 1099 payees, or large unposted journal sets.

When `ANTHROPIC_API_KEY` is set and `--backend` is not specified, the runner
auto-selects the Anthropic backend.

---

### Backend 2 — Local Qwen via llama-server (OpenAI-compatible)

Best for: privacy-sensitive clients, air-gapped deployments, cost elimination
at the expense of some reasoning quality. Requires <host> (2x Tesla P40) or
equivalent GPU server running `llama-server` with the
`Qwen3.6-35B-A3B-Vision` model.

```bash
export LLM_ENDPOINT_URL="http://<host>:8088/v1"
export LLM_MODEL="Qwen3.6-35B-A3B-Vision"
export LLM_API_KEY="local-qwen"   # placeholder; llama-server ignores it

uv run python agents/tax_specialists/form_1120_preparer/run_specialist.py \
    --backend      openai \
    --workspace-id 550e8400-e29b-41d4-a716-446655440000 \
    --entity-id    aabbccdd-1234-5678-9abc-000000000001 \
    --tax-year     2023 \
    --output       ./drafts/mu-inc-2023-1120-draft.md \
    --verbose
```

When `LLM_ENDPOINT_URL` is set and `ANTHROPIC_API_KEY` is not, the runner
auto-selects the OpenAI backend.

#### If the local model does not support function calling

Some quantised local models do not reliably advertise function-calling
support. Use `--instruction-format json` to switch to a structured-text
fallback where tool calls are expressed as `TOOL_CALL: {...}` lines in
the model's output:

```bash
uv run python agents/tax_specialists/form_1120_preparer/run_specialist.py \
    --backend             openai \
    --instruction-format  json \
    --workspace-id        550e8400-e29b-41d4-a716-446655440000 \
    --entity-id           aabbccdd-1234-5678-9abc-000000000001 \
    --tax-year            2023 \
    --output              ./drafts/mu-inc-2023-1120-draft.md
```

---

## All CLI flags

| Flag | Default | Description |
|---|---|---|
| `--workspace-id` | required | Workspace UUID |
| `--entity-id` | required | Entity UUID within the workspace |
| `--tax-year` | required | Four-digit tax year (e.g. 2023) |
| `--output` | required | Output path for the DRAFT markdown file |
| `--backend` | auto | `anthropic` or `openai`. Auto-detected from env vars if omitted. |
| `--model` | `sonnet` | Anthropic model: `sonnet` or `opus`. Ignored for `--backend openai`. |
| `--mcp-url` | `$ACCTOPS_MCP_URL` | Override MCP endpoint base URL |
| `--llm-endpoint-url` | `$LLM_ENDPOINT_URL` | Base URL for OpenAI-compatible local endpoint |
| `--llm-model` | `$LLM_MODEL` | Model name at the local endpoint |
| `--llm-api-key` | `$LLM_API_KEY` | API key for the local endpoint |
| `--instruction-format` | `tools` | `tools` (native fn-calling) or `json` (text fallback) |
| `--verbose` | off | Stream tool-use events and config to stdout |

---

## Getting a service token

```bash
curl -s \
  -d "client_id=accounting-ops-app" \
  -d "client_secret=<secret>" \
  -d "grant_type=client_credentials" \
  "https://auth.example.com/realms/uchub/protocol/openid-connect/token" \
  | jq -r .access_token
```

---

## Trade-offs: Anthropic vs. local Qwen

| Dimension | Anthropic (Claude Sonnet/Opus) | Local Qwen3.6-35B |
|---|---|---|
| **Reasoning quality** | High; Opus is best in class for multi-step tax logic | Good for straightforward returns; weaker on novel judgment calls |
| **Privacy** | Data leaves the network to Anthropic | All data stays on-prem; suitable for privacy-sensitive clients |
| **Cost** | ~$3–15 per run (Sonnet vs. Opus, 8K output) | Near-zero marginal cost after GPU capex |
| **Latency** | 30–90 s typical | 2–5 min on P40 for a full return |
| **Tool calling** | Native MCP integration via Anthropic SDK beta | Manual loop; requires OpenAI function-calling support from the model or JSON-mode fallback |
| **Context window** | 200K (Sonnet/Opus 4) | 32K–128K depending on quantisation |
| **Iteration cap** | SDK-managed (no hard cap) | 30 iterations (configurable in `_MAX_LOOP_ITERATIONS`) |
| **Reliability** | Production-grade | Dependent on local GPU uptime (<host>); no SLA |
| **Best for** | Production, complex returns, CPA deliverables | Dogfood, privacy-sensitive, cost experimentation |

### Known quirks with local models

1. **Function-calling availability**: Not all quantised GGUF builds of Qwen
   expose a working `tools` parameter. If you see the model ignore tool calls
   or return an empty draft, try `--instruction-format json`.

2. **JSON-mode reliability**: Smaller quantisations (Q4_K_M and below) sometimes
   emit malformed `TOOL_CALL:` lines. Use Q6_K or above for production-quality
   JSON mode output.

3. **Iteration cap**: The OpenAI path caps at 30 tool-call iterations
   (`_MAX_LOOP_ITERATIONS`). A full 1120 run typically uses 6–12 iterations
   (5 tool calls + a final response turn). The cap is a safety rail, not a
   normal boundary; if you're consistently hitting it, the model may be looping
   on a failed tool call — check `--verbose` output.

4. **Context exhaustion**: At 32K context, very long journal sets returned by
   `get_period_journal` may fill the context window before the model produces
   the final Markdown draft. Prefer larger-context quantisations or limit the
   date range passed to `get_period_journal`.

---

## Architecture

Both backends share:
- The same `system_prompt.md` (verbatim system turn).
- The same `_build_user_message()` user turn.
- The same `_ALLOWED_TOOLS` list (least-privilege MCP filter).
- The same output schema (Markdown DRAFT with CPA-flagged items footer).

What differs:

```
Anthropic path:
    run_specialist.py
        └── anthropic.Anthropic().beta.messages.create(mcp_servers=[...])
                 │
                 └── Anthropic API manages the tool-call loop internally

OpenAI path:
    run_specialist.py
        ├── mcp.client.streamable_http  ──>  /mcp (FastMCP)
        │       list_tools() / call_tool()
        └── openai.AsyncOpenAI(base_url=local)
                 │
                 └── Manual loop: LLM response -> tool dispatch -> append -> repeat
```

For a more complex multi-agent workflow (e.g. a CPA review agent that reads
the `tax_review_queue` and responds to flags), the same pattern applies: write
a new `run_specialist.py` with a different system prompt and `_ALLOWED_TOOLS`
list. The backend selection and MCP tool-call loop are reusable.

---

## Running the smoke tests

```bash
uv run --extra dev pytest tests/smoke/test_runner_backend_selection.py -v
```

These tests run offline (no network, no LLM, no MCP server required).

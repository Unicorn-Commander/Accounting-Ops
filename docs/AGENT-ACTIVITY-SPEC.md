# Agent Activity Feed — Implementation Spec

Synthesized from research swarm 2026-05-21. Source of truth for the activity feed UI.

## Verdict colors

| Verdict | Color | Behavior |
|---|---|---|
| APPROVE | green | row collapses by default, hover to peek transparency |
| REJECT | red | always shows reason in collapsed row |
| NEEDS_REVIEW | amber | row expanded by default, action buttons inline |
| BLOCKED | orange | shows blocker + [Resolve] link |
| THINKING | blue + pulse | shows current sub-step + elapsed counter; updates in place |
| CANCELED | gray | folded into "X canceled" group |

## Materiality bands (tenant-configurable defaults)

- **L:** < $500, agent auto-approves if confidence = "Confident"
- **M:** $500-$10k, auto-approves if Confident + within-policy
- **H:** ≥ $10k or any cross-month/cross-period entry, always NEEDS_REVIEW

## "Thinking" pattern (no fake progress bars)

1. **0-10s:** blue dot pulse + action text. No timer (avoids quick-task anxiety).
2. **10-60s:** timer appears, sub-step text streams. After 15s of no sub-step change, append "still working" (never "almost done").
3. **60s+:** amber side stripe + [Backgrounding] chip. Click moves to collapsed "Long-running" group. [Cancel] button appears.
4. **30min+:** auto-flips to NEEDS_REVIEW with "Exceeded normal duration; verify agent is healthy."

## Real-time update strategy

**Primary: SSE** at `GET /api/v1/activity/stream?since=<Last-Event-ID>&filters=...`
- `event: row.upsert` — full row JSON
- `event: row.update` — sub-step + elapsed_ms every 2s while THINKING
- `event: heartbeat` — every 15s
- `event: pulse` — every 5s with `{thinking, awaiting, auto_approved_24h}` counts

**Fallback:** if SSE fails twice in 60s, switch to polling `GET /api/v1/activity?since=<ts>` every 4s with "Reconnecting (polling)" pill. Auto-retry SSE every 30s.

**Why not WebSocket:** feed is one-way. Approvals are ordinary POSTs. SSE survives proxies (Traefik, Cloudflare); WS often gets dropped.

## Row schema

```sql
event_id            uuid (Last-Event-ID for SSE resume)
emitted_at          timestamptz
agent_id            text  -- "recon-1.2", "gl-post", "ap-pay"
agent_version       text
tenant_id           uuid
action              text  -- verb + object, max 120
materiality         enum  L | M | H
amount_cents        bigint nullable
verdict             enum  APPROVE | REJECT | NEEDS_REVIEW | BLOCKED | THINKING | CANCELED
decision_basis      jsonb  -- {rule_id, model_id, confidence_band, policy_refs[]}
transparency        jsonb  -- {sql[], python[], prompt_id, tool_calls[], inputs, outputs}
parent_run_id       uuid   -- groups multi-step tasks
elapsed_ms          int    -- THINKING rows update in place
needs_human         bool
resolved_by         text nullable
resolved_at         timestamptz nullable
```

Append-only. Verdict changes append a new row referencing `parent_run_id`.

## Grouping modes (toggle top-right)

- **Time** (default): newest first, infinite scroll
- **By task**: rows roll up under `parent_run_id`, aggregate verdict + total $
- **By agent**: collapsed per `agent_id`, ordered by `needs_human` count desc
- **By tenant**: required for users with cross-tenant access; respects RBAC

## Empty + error states

- **No activity in window:** "Nothing's happened yet. Once your agents start working, every decision will show up here in real time."
- **Filter returns zero:** "No rows match these filters. Clear filters"
- **New tenant first-run:** "Welcome. Your agents are idle. Connect a ledger source to wake them up."
- **SSE disconnected:** orange pill "Reconnecting (polling every 4s)" — no modal
- **Detail fetch fails:** inline banner "Couldn't load this decision's reasoning. Retry"
- **Stale data (>60s no events):** header pill "Feed paused 1m ago" with [Reconnect]
- **Permission denied on row:** opaque gray row with "You don't have access to this tenant" — no leak of action/amount

## Audit + compliance

- All rows immutable
- Transparency artifacts persisted 7 years (HIPAA tenants get isolated storage path)
- Export CSV is RBAC-gated and watermarked with viewer's `uc_uid`

// Pure, client-safe mapper from the real AIActivityRow (FastAPI) shape to the
// AgentDecision shape the stream renders. NO server-only imports here (type-only
// import of AIActivityRow is erased at build) so this module is safe to use in
// both the server component (initial load) and the client polling hook.
//
// Honesty rule: map ONLY fields the backend actually provides. We deliberately
// DO NOT fabricate confidence, materiality, or dollar amounts — those slots stay
// empty when the source row doesn't carry them, and the card hides them. A real,
// plainer card beats a rich, invented one.

import type { AIActivityRow } from "@/lib/ai-activity";
import type { AgentDecision, AgentStatus } from "./types";

function mapStatus(outcome: string): AgentStatus {
  const o = (outcome || "").toLowerCase();
  if (["blocked", "rejected", "error", "denied"].includes(o)) return "block";
  if (["needs_review", "queued", "pending", "awaiting"].includes(o)) return "await";
  return "appr";
}

function statusLabel(row: AIActivityRow, status: AgentStatus): string {
  if (status === "block") return row.outcome || "blocked";
  if (status === "await") return "needs you";
  // appr
  if (row.auto_applied) return "auto-approved";
  return row.outcome || "applied";
}

// Strip an LLM provider prefix and tidy the agent id into a readable label.
// "anthropic/cpa-tax-resolver" -> "cpa-tax-resolver"
export function humanizeAgent(agentId: string): string {
  if (!agentId) return "agent";
  const seg = agentId.includes("/") ? agentId.split("/").pop()! : agentId;
  return seg.trim();
}

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("en-US", { hour12: false });
  } catch {
    return "";
  }
}

export function activityRowToDecision(row: AIActivityRow): AgentDecision {
  const status = mapStatus(row.outcome);
  return {
    id: row.id,
    ag: row.agent_id,
    agentLabel: humanizeAgent(row.agent_id),
    title: row.action_label || row.tool_name || "Agent action",
    why: row.reasoning_trace || "",
    // artifact is real provenance — surface its label when present.
    receipt: row.artifact?.label || undefined,
    ent: row.entity_display_name || undefined,
    status,
    statusLabel: statusLabel(row, status),
    timestamp: fmtTime(row.created_at),
    reversible: row.reversible,
  };
}

export function countByStatus(decisions: AgentDecision[]) {
  return {
    thinking: 0,
    awaiting: decisions.filter((d) => d.status === "await").length,
    autoApproved: decisions.filter((d) => d.status === "appr").length,
    blocked: decisions.filter((d) => d.status === "block").length,
  };
}

// AgentDecision is rendered from TWO sources:
//   1. real backend rows (AIActivityRow) mapped in map-activity.ts — these carry
//      only a subset of fields, so most visual fields are OPTIONAL and the card
//      hides what's absent (no fabricated confidence / materiality / amounts).
//   2. the gated demo simulation (use-agent-stream, demo mode) — fully populated.
// `ag` is the raw agent id (real backend agent_id OR a demo key); agent-config's
// resolveAgent() turns any string into a stable avatar/color.

export type AgentStatus = "appr" | "await" | "block" | "think";

export type AgentDecision = {
  id: string;
  ag: string;
  agentLabel?: string;
  think?: string;
  title: string;
  why: string;
  diff?: [string, string];
  rule?: string;
  receipt?: string;
  conf?: number;
  ent?: string;
  matl?: "L" | "M" | "H";
  amt?: string;
  cr?: boolean;
  status: AgentStatus;
  statusLabel?: string;
  reversible?: boolean;
  timestamp: string;
};

export type AgentStreamCounts = {
  thinking: number;
  awaiting: number;
  autoApproved: number;
  blocked: number;
};

export type AgentStreamState = {
  decisions: AgentDecision[];
  counts: AgentStreamCounts;
};

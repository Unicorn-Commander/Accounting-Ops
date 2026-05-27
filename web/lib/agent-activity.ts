// Shared data source + types for the agent-activity feed.
//
// Both the inline dashboard section (`AgentActivityFeed`) and the full
// `/agent-activity` page read from here, so there is exactly one source of
// truth for the demo events and the verdict vocabulary.
//
// Phase 1 replaces ACTIVITY_EVENTS with a live SSE connection to
//   GET /api/v1/activity/stream?since=<Last-Event-ID>&filters=...
// from the FastAPI orchestrator. The shape below intentionally mirrors the
// fields that stream is expected to emit so the swap is contract-only.

export type Verdict =
  | "APPROVE"
  | "REJECT"
  | "NEEDS_REVIEW"
  | "BLOCKED"
  | "THINKING"
  | "CANCELED";

export interface AgentEvent {
  time: string;
  agent: string;
  action: string;
  tenant: string;
  amount?: string;
  materiality?: "L" | "M" | "H";
  verdict: Verdict;
  reasoning?: string;
}

export const ACTIVITY_EVENTS: AgentEvent[] = [
  { time: "14:22:08", agent: "recon-1.2", action: "Matched 47 stripe payouts to invoice batch 4f2c", tenant: "MU Inc", amount: "$12,408.22", materiality: "M", verdict: "APPROVE", reasoning: "All 47 payouts within $0.01 tolerance of expected. Counterparty match confidence 0.97." },
  { time: "14:22:09", agent: "compliance", action: "Flagged vendor payment for 1099-NEC threshold", tenant: "MU Inc", amount: "$420.00", materiality: "L", verdict: "NEEDS_REVIEW", reasoning: "Cumulative payments to Daniel Tran now $612 (was $192). Crosses $600 reporting threshold for tax year 2026." },
  { time: "14:22:12", agent: "bookkeeper", action: "Categorized Anthropic.com to SaaS Subscriptions", tenant: "MU Inc", amount: "$200.00", materiality: "L", verdict: "APPROVE", reasoning: "Matched prior pattern. Confidence 0.97. Below review threshold." },
  { time: "14:22:14", agent: "tax-rules", action: "Computing Q1 NJ sales tax nexus...", tenant: "MU Inc", verdict: "THINKING", reasoning: "Aggregating trailing 12mo revenue across 6 states. ETA 8s." },
  { time: "14:22:18", agent: "recon-1.2", action: "Matched 12 Mercury debits to expense report", tenant: "MU Inc", materiality: "L", verdict: "APPROVE" },
  { time: "14:22:22", agent: "ap-pay", action: "ACH to Soylent Corp — beneficiary unverified", tenant: "MU Inc", amount: "$12,400.00", materiality: "H", verdict: "BLOCKED", reasoning: "Beneficiary 'Soylent Corp' not in vendor allowlist. Manual verification required before proceeding." },
  { time: "14:21:53", agent: "bookkeeper", action: "Categorized Linear to SaaS Subscriptions", tenant: "MU Inc", amount: "$50.00", materiality: "L", verdict: "APPROVE" },
  { time: "14:21:42", agent: "fers-watch", action: "Aaron personal · YTD earned income at 62% of threshold", tenant: "Aaron personal", verdict: "APPROVE", reasoning: "Below 70% warning level. Monitoring." },
  { time: "14:21:30", agent: "bookkeeper", action: "Categorized eBay payout to Marketplace Revenue", tenant: "MU Inc", amount: "$1,847.81", materiality: "M", verdict: "APPROVE" },
  { time: "14:21:18", agent: "recon-1.2", action: "Unable to match $48.20 charge — vendor unknown", tenant: "MU Inc", amount: "$48.20", materiality: "L", verdict: "NEEDS_REVIEW", reasoning: "No prior pattern. Description 'AC*FAST*OVERFLOW' might be Claude API overflow billing. Asking for confirmation." },
  { time: "14:20:55", agent: "compliance", action: "Sales tax check: TX threshold approaching ($487k of $500k)", tenant: "MU Inc", materiality: "M", verdict: "NEEDS_REVIEW", reasoning: "Trailing 12mo gross sales into TX at $487k. Nexus threshold $500k. Recommend monitoring; no immediate action." },
  { time: "14:20:42", agent: "bookkeeper", action: "Posted journal entry · Wise USDC payout to Brazil", tenant: "Majiks JV", amount: "$612.55", materiality: "L", verdict: "APPROVE", reasoning: "Tagged for FBAR/FATCA review per foreign_payment_flag rule." }
];

export interface ActivitySummary {
  thinking: number;
  awaiting: number;
  blocked: number;
}

export function summarizeActivity(events: AgentEvent[]): ActivitySummary {
  return {
    thinking: events.filter((e) => e.verdict === "THINKING").length,
    awaiting: events.filter((e) => e.verdict === "NEEDS_REVIEW").length,
    blocked: events.filter((e) => e.verdict === "BLOCKED").length
  };
}

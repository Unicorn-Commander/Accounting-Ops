"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { AIActivityResponse } from "@/lib/ai-activity";
import { activityRowToDecision, countByStatus } from "./map-activity";
import type { AgentDecision, AgentStreamState } from "./types";

const MAX_VISIBLE = 6;
const POLL_MS = 6000;

// Demo simulation is ONLY active behind this build-time flag. By default the
// stream shows real agent decisions (or a clean empty state) — never fabricated
// financial activity.
const DEMO = process.env.NEXT_PUBLIC_AGENT_STREAM_DEMO === "true";

// ---------------------------------------------------------------------------
// Demo data (gated) — used for screenshots / design review only.
// ---------------------------------------------------------------------------
const DEMO_DECISIONS: AgentDecision[] = [
  { id: "d1", ag: "recon-1.2", agentLabel: "recon-1.2", title: "Matched 47 Stripe payouts to invoice batch 412c", why: "All 47 within $0.01 of expected. Counterparty match confidence 0.97.", rule: "rule: payout-tolerance ±$0.01", conf: 97, ent: "MUI Inc", matl: "L", amt: "+$4,212.00", cr: true, status: "appr", timestamp: "14:32:18" },
  { id: "d2", ag: "bookkeeper", agentLabel: "bookkeeper", title: "Categorized Anthropic, PBC → SaaS subscriptions", why: "Vendor matches 6 prior entries. Below your $300 review threshold.", diff: ["Uncategorized", "SaaS subscriptions"], receipt: "anthropic-invoice.pdf", conf: 96, ent: "MUI Inc", matl: "L", amt: "$200.00", status: "appr", timestamp: "14:28:05" },
  { id: "d3", ag: "compliance", agentLabel: "compliance", title: "Flagged Daniel Tran — crossed 1099-NEC threshold", why: "YTD payments now $812 (was $192). Reporting required for 2026.", rule: "§ 1099-NEC · >$600/yr", conf: 88, ent: "MUI Inc", matl: "M", amt: "$420.00", status: "await", timestamp: "14:15:42" },
  { id: "d4", ag: "tax-rules", agentLabel: "tax-rules", title: "Computing Q1 NJ sales-tax nexus", why: "Trailing revenue across 6 states. Economic-nexus check in progress.", rule: "NJ · econ-nexus $100k / 200 txns", conf: 71, ent: "MUI Inc", matl: "M", amt: "—", status: "await", timestamp: "14:08:33" },
  { id: "d5", ag: "recon-1.2", agentLabel: "recon-1.2", title: "Matched 12 Mercury debits to expense report", why: "Travel + meals, all under per-diem. Receipts attached automatically.", diff: ["Uncategorized", "Travel & meals"], receipt: "mercury-stmt.csv", conf: 94, ent: "MUI Inc", matl: "L", amt: "$1,318.40", status: "appr", timestamp: "13:55:10" },
  { id: "d6", ag: "ap-pay", agentLabel: "ap-pay", title: "Held ACH to Soylent Corp — beneficiary unverified", why: "Not on your allowlist. Manual verification required before proceeding.", rule: "rule: allowlist-only ACH", conf: 42, ent: "MUI Inc", matl: "H", amt: "$12,400.00", status: "block", timestamp: "13:42:01" },
];
const DEMO_THINKING: AgentDecision = {
  id: "thinking-1", ag: "bookkeeper", agentLabel: "bookkeeper",
  think: "an Adobe subscription charge that hit the Mercury card — matching it against the prior 11 months to decide whether it's Software & tools or Uncategorized…",
  title: "", why: "", conf: 30, status: "think", timestamp: "now",
};
const DEMO_POOL = [3, 4, 5, 6, 2, 0, 1];

export function useAgentStream(opts: {
  workspaceId?: string;
  entityId?: string;
  initialDecisions?: AgentDecision[];
} = {}) {
  const { workspaceId, entityId, initialDecisions = [] } = opts;

  const [state, setState] = useState<AgentStreamState>(() => {
    if (DEMO) {
      return { decisions: [...DEMO_DECISIONS], counts: { thinking: 1, awaiting: 1, autoApproved: 47, blocked: 1 } };
    }
    const decisions = initialDecisions.slice(0, MAX_VISIBLE);
    return { decisions, counts: countByStatus(decisions) };
  });

  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const poolPosRef = useRef(0);
  const seenRef = useRef<Set<string>>(new Set(initialDecisions.map((d) => d.id)));

  // ---- demo tick (gated) ----
  const demoTick = useCallback(() => {
    setState((prev) => {
      const idx = DEMO_POOL[poolPosRef.current++ % DEMO_POOL.length];
      const job = DEMO_DECISIONS[idx];
      if (!job) return prev;
      const reKeyed: AgentDecision = { ...job, id: `stream-${poolPosRef.current}-${idx}` };
      const updated = [reKeyed, ...prev.decisions.filter((d) => d.status !== "think")].slice(0, MAX_VISIBLE);
      const withThinking = [{ ...DEMO_THINKING, id: `think-${poolPosRef.current}` }, ...updated].slice(0, MAX_VISIBLE);
      return {
        decisions: withThinking,
        counts: {
          thinking: 1,
          awaiting: updated.filter((d) => d.status === "await").length,
          autoApproved: prev.counts.autoApproved + (reKeyed.status === "appr" ? 1 : 0),
          blocked: updated.filter((d) => d.status === "block").length,
        },
      };
    });
  }, []);

  // ---- real poll ----
  const poll = useCallback(async () => {
    if (!workspaceId) return;
    const params = new URLSearchParams();
    if (entityId) params.set("entity_id", entityId);
    params.set("limit", String(MAX_VISIBLE));
    try {
      const res = await fetch(
        `/api/workspaces/${workspaceId}/ai-activity?${params.toString()}`,
        { cache: "no-store" }
      );
      if (!res.ok) return;
      const data = (await res.json()) as AIActivityResponse;
      const decisions = (data.rows ?? []).map(activityRowToDecision).slice(0, MAX_VISIBLE);
      decisions.forEach((d) => seenRef.current.add(d.id));
      setState({ decisions, counts: countByStatus(decisions) });
    } catch {
      // best-effort: keep prior data on a failed poll
    }
  }, [workspaceId, entityId]);

  const startStream = useCallback(() => {
    if (timerRef.current) return;
    if (DEMO) {
      // seed a thinking card, then cycle
      setState((prev) => ({ ...prev, decisions: [DEMO_THINKING, ...prev.decisions].slice(0, MAX_VISIBLE) }));
      timerRef.current = setInterval(demoTick, 4200);
      return;
    }
    // real mode: poll the live endpoint
    void poll();
    timerRef.current = setInterval(() => void poll(), POLL_MS);
  }, [demoTick, poll]);

  const stopStream = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => stopStream, [stopStream]);

  return { state, startStream, stopStream, isDemo: DEMO };
}

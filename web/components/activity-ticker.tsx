"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown, X } from "lucide-react";
import { cn } from "@/lib/cn";

type Verdict = "APPROVE" | "REJECT" | "NEEDS_REVIEW" | "BLOCKED" | "THINKING" | "CANCELED";

interface ActivityRow {
  id: string;
  time: string;
  agent: string;
  action: string;
  verdict: Verdict;
  materiality?: "L" | "M" | "H";
  amount?: string;
}

const VERDICT_COLOR: Record<Verdict, string> = {
  APPROVE: "text-credit",
  REJECT: "text-debit",
  NEEDS_REVIEW: "text-pending",
  BLOCKED: "text-blocked",
  THINKING: "text-agent-500",
  CANCELED: "text-ink-tertiary"
};

const SEED: ActivityRow[] = [
  { id: "1", time: "14:22:08", agent: "recon-1.2", action: "Matched 47 stripe payouts to invoice batch", verdict: "APPROVE", materiality: "M", amount: "$12,408.22" },
  { id: "2", time: "14:22:09", agent: "compliance", action: "Flagged vendor payment for 1099-NEC threshold (cumulative $612)", verdict: "NEEDS_REVIEW", materiality: "L", amount: "$420.00" },
  { id: "3", time: "14:22:12", agent: "bookkeeper", action: "Categorized Anthropic.com to SaaS Subscriptions", verdict: "APPROVE", materiality: "L", amount: "$200.00" },
  { id: "4", time: "14:22:14", agent: "tax-rules", action: "Computing Q1 NJ sales tax nexus...", verdict: "THINKING" },
  { id: "5", time: "14:22:18", agent: "recon-1.2", action: "Matched 12 Mercury debits to expense report", verdict: "APPROVE", materiality: "L" },
  { id: "6", time: "14:22:22", agent: "ap-pay", action: "ACH to Soylent Corp — beneficiary unverified", verdict: "BLOCKED", materiality: "H", amount: "$12,400.00" },
  { id: "7", time: "14:22:25", agent: "bookkeeper", action: "Categorized Linear → SaaS Subscriptions", verdict: "APPROVE", materiality: "L", amount: "$50.00" }
];

export function ActivityTicker({ defaultOpen = true }: { defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const [rows, setRows] = useState<ActivityRow[]>(SEED);
  const counter = useRef(SEED.length);

  // Simulate live activity for the demo (Phase 1 wires real SSE)
  useEffect(() => {
    if (!open) return;
    const samples: Array<Omit<ActivityRow, "id" | "time">> = [
      { agent: "bookkeeper", action: "Categorized Cursor → Developer Tools", verdict: "APPROVE", materiality: "L", amount: "$20.00" },
      { agent: "recon-1.2", action: "Reconciling daily Stripe payout batch", verdict: "THINKING" },
      { agent: "compliance", action: "Sales tax nexus check: TX threshold approaching ($487k)", verdict: "NEEDS_REVIEW", materiality: "M" },
      { agent: "bookkeeper", action: "Categorized AWS → Infrastructure", verdict: "APPROVE", materiality: "L", amount: "$1,247.18" }
    ];
    const interval = setInterval(() => {
      counter.current += 1;
      const next = samples[counter.current % samples.length];
      const now = new Date();
      const time = now.toLocaleTimeString("en-US", { hour12: false });
      setRows((prev) => [
        { id: String(counter.current), time, ...next },
        ...prev.slice(0, 9)
      ]);
    }, 6500);
    return () => clearInterval(interval);
  }, [open]);

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-4 right-4 z-40 flex items-center gap-2 rounded-full border border-default bg-elevated/95 px-3 py-2 text-xs text-ink-secondary shadow-ticker backdrop-blur hover:bg-elevated"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-agent-500 animate-pulse-soft shadow-agent-glow" />
        Agent activity
        <ChevronDown size={14} className="rotate-180" />
      </button>
    );
  }

  return (
    <aside className="fixed bottom-4 right-4 z-40 w-96 max-w-[calc(100vw-2rem)] overflow-hidden rounded-lg border border-default bg-elevated/95 shadow-ticker backdrop-blur">
      <header className="flex items-center gap-2 border-b border-subtle px-3 py-2">
        <span className="h-1.5 w-1.5 rounded-full bg-agent-500 animate-pulse-soft shadow-agent-glow" />
        <p className="text-xs font-semibold">Agent activity</p>
        <span className="ml-auto font-mono text-[10px] text-ink-tertiary">live</span>
        <button
          onClick={() => setOpen(false)}
          className="ml-1 rounded p-0.5 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
          aria-label="Collapse activity ticker"
        >
          <X size={13} />
        </button>
      </header>
      <ul className="max-h-80 space-y-0.5 overflow-y-auto p-2 font-mono text-[11px] leading-relaxed">
        {rows.map((row) => (
          <li
            key={row.id}
            className={cn(
              "rounded px-2 py-1.5 transition",
              row.verdict === "NEEDS_REVIEW"
                ? "bg-pending/5 border-l-2 border-pending/60"
                : row.verdict === "BLOCKED"
                  ? "bg-blocked/5 border-l-2 border-blocked/60"
                  : "hover:bg-white/[0.02]"
            )}
          >
            <div className="flex items-start gap-2">
              <span className="shrink-0 text-ink-tertiary">{row.time}</span>
              <span className={cn("shrink-0 font-semibold", VERDICT_COLOR[row.verdict])}>{row.agent}</span>
              {row.materiality ? (
                <span className="shrink-0 font-mono text-[9px] text-ink-tertiary">[{row.materiality}]</span>
              ) : null}
              <span className="text-ink-secondary">{row.action}</span>
            </div>
            <div className="mt-0.5 ml-1 flex items-center gap-2 text-[9px] uppercase tracking-wider">
              <span className={VERDICT_COLOR[row.verdict]}>{row.verdict}</span>
              {row.amount ? (
                <span className="text-ink-tertiary">{row.amount}</span>
              ) : null}
              {row.verdict === "THINKING" ? (
                <span className="text-agent-500">working...</span>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
      <div className="border-t border-subtle px-3 py-1.5 text-[10px] text-ink-tertiary">
        Demo data. Phase 1 wires real SSE feed.
      </div>
    </aside>
  );
}

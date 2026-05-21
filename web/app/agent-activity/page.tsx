import { AlertCircle, CheckCircle2, Filter, MoreHorizontal, RotateCw, ShieldAlert, XCircle } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { Card, MaterialityBadge, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { cn } from "@/lib/cn";

type Verdict = "APPROVE" | "REJECT" | "NEEDS_REVIEW" | "BLOCKED" | "THINKING" | "CANCELED";

interface Row {
  time: string;
  agent: string;
  action: string;
  tenant: string;
  amount?: string;
  materiality?: "L" | "M" | "H";
  verdict: Verdict;
  reasoning?: string;
}

const ROWS: Row[] = [
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

const VERDICT_STYLES: Record<Verdict, { bg: string; border: string; text: string; icon: React.ReactNode }> = {
  APPROVE: {
    bg: "bg-credit/[0.03]",
    border: "border-l-credit/30",
    text: "text-credit",
    icon: <CheckCircle2 size={14} />
  },
  REJECT: {
    bg: "bg-debit/[0.05]",
    border: "border-l-debit/40",
    text: "text-debit",
    icon: <XCircle size={14} />
  },
  NEEDS_REVIEW: {
    bg: "bg-pending/[0.06]",
    border: "border-l-pending/60",
    text: "text-pending",
    icon: <AlertCircle size={14} />
  },
  BLOCKED: {
    bg: "bg-blocked/[0.06]",
    border: "border-l-blocked/60",
    text: "text-blocked",
    icon: <ShieldAlert size={14} />
  },
  THINKING: {
    bg: "bg-agent-500/[0.05]",
    border: "border-l-agent-500/50",
    text: "text-agent-500",
    icon: <RotateCw size={14} className="animate-spin" />
  },
  CANCELED: {
    bg: "",
    border: "border-l-ink-disabled/30",
    text: "text-ink-tertiary",
    icon: <MoreHorizontal size={14} />
  }
};

export default function AgentActivityPage() {
  const thinkingCount = ROWS.filter((r) => r.verdict === "THINKING").length;
  const awaitingCount = ROWS.filter((r) => r.verdict === "NEEDS_REVIEW").length;
  const blockedCount = ROWS.filter((r) => r.verdict === "BLOCKED").length;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Agent activity"
          subtitle="Every decision your agents make, in real time, with reasoning. Append-only. Tamper-evident."
          action={
            <button className="inline-flex items-center gap-1.5 rounded-md border border-default bg-surface px-3 py-1.5 text-xs text-ink-secondary hover:bg-elevated">
              <Filter size={13} /> Filters
            </button>
          }
        />

        <Card padded={false}>
          <div className="flex flex-wrap items-center gap-3 border-b border-subtle px-5 py-3">
            <div className="flex items-center gap-2 text-xs">
              <span className="h-1.5 w-1.5 rounded-full bg-agent-500 animate-pulse-soft shadow-agent-glow" />
              <span className="font-mono text-ink-secondary">{thinkingCount} thinking</span>
            </div>
            <span className="text-ink-disabled">·</span>
            <div className="flex items-center gap-2 text-xs">
              <span className="h-1.5 w-1.5 rounded-full bg-pending" />
              <span className="font-mono text-ink-secondary">{awaitingCount} awaiting approval</span>
            </div>
            <span className="text-ink-disabled">·</span>
            <div className="flex items-center gap-2 text-xs">
              <span className="h-1.5 w-1.5 rounded-full bg-blocked" />
              <span className="font-mono text-ink-secondary">{blockedCount} blocked</span>
            </div>
            <span className="text-ink-disabled">·</span>
            <div className="flex items-center gap-2 text-xs">
              <span className="h-1.5 w-1.5 rounded-full bg-credit" />
              <span className="font-mono text-ink-secondary">47 auto-approved today</span>
            </div>
            <span className="ml-auto text-[11px] text-ink-tertiary">
              Last 60 minutes · SSE live
            </span>
          </div>

          <ul className="divide-y divide-subtle/60">
            {ROWS.map((row, i) => {
              const style = VERDICT_STYLES[row.verdict];
              return (
                <li
                  key={i}
                  className={cn(
                    "flex gap-4 border-l-2 px-5 py-3 transition hover:bg-white/[0.02]",
                    style.bg,
                    style.border
                  )}
                >
                  <div className="shrink-0 pt-0.5">
                    <span className={style.text}>{style.icon}</span>
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex flex-wrap items-baseline gap-x-3">
                      <span className="font-mono text-xs text-ink-tertiary shrink-0">{row.time}</span>
                      <span className={cn("font-mono text-xs font-semibold", style.text)}>
                        {row.agent}
                      </span>
                      <span className="font-mono text-xs text-ink-tertiary">·</span>
                      <span className="text-sm text-ink-primary">{row.action}</span>
                    </div>
                    {row.reasoning && (
                      <p className="mt-1 font-reasoning text-xs leading-relaxed text-ink-secondary">
                        {row.reasoning}
                      </p>
                    )}
                    <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px]">
                      <span className="font-mono text-ink-tertiary">{row.tenant}</span>
                      {row.materiality && <MaterialityBadge level={row.materiality} />}
                      {row.amount && (
                        <span className="font-mono text-ink-secondary">{row.amount}</span>
                      )}
                      <span className={cn("ml-auto text-[10px] uppercase tracking-wider", style.text)}>
                        {row.verdict.replace("_", " ")}
                      </span>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>

          <div className="flex items-center justify-between border-t border-subtle px-5 py-3 text-[11px] text-ink-tertiary">
            <span>Showing 12 of 247 events today</span>
            <span>Phase 1 wires real SSE stream from FastAPI</span>
          </div>
        </Card>
      </PageShell>
    </AppShell>
  );
}

import { Check, ChevronRight, MessageCircle, X } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import {
  Card,
  ConfidenceDot,
  MaterialityBadge,
  PageShell,
  SectionHeader,
  StatusChip
} from "@/components/primitives";

const COLUMNS = [
  {
    title: "Needs Review",
    tone: "pending",
    count: 4,
    items: [
      { id: 1, title: "Mercury ACH inbound · $5,000", body: "Looks like inter-entity transfer between Magic Unicorn Inc and Majiks JV. No matching outbound found in Mercury Majiks JV account.", conf: 0.62, mat: "M", agent: "recon-1.2" },
      { id: 2, title: "Cisco Meraki licensing · $420", body: "Annual or monthly? Last year was annual ($420 vs $35x12). Need confirmation.", conf: 0.71, mat: "L", agent: "bookkeeper" },
      { id: 3, title: "Wise USD->USDC payout · $612", body: "Recipient Daniel Tran. Crosses $600 1099-NEC cumulative threshold ($487 + $612 = $1,099).", conf: 0.85, mat: "L", agent: "compliance" },
      { id: 4, title: "Stripe dispute · -$200", body: "Anthropic charge contested. Possible duplicate?", conf: 0.55, mat: "L", agent: "bookkeeper" }
    ]
  },
  {
    title: "Agent Suggested",
    tone: "agent",
    count: 4,
    items: [
      { id: 5, title: "Anthropic, PBC · $200", body: "Suggest: 6210·SaaS·AI. Matches 4 prior entries.", conf: 0.97, mat: "L", agent: "bookkeeper" },
      { id: 6, title: "Hostinger · $8.99", body: "Suggest: 6100·Infrastructure·VPS. Matches 12 prior entries.", conf: 0.99, mat: "L", agent: "bookkeeper" },
      { id: 7, title: "Postmark · $15", body: "Suggest: 6230·SaaS·Email. Matches 6 prior entries.", conf: 0.98, mat: "L", agent: "bookkeeper" },
      { id: 8, title: "AWS · $87.42", body: "Suggest: 6100·Infrastructure·CDN. Matches 18 prior entries.", conf: 0.94, mat: "L", agent: "bookkeeper" }
    ]
  },
  {
    title: "Approved (today)",
    tone: "credit",
    count: 23,
    items: [
      { id: 9, title: "Stripe payout · $12,408", body: "Posted 14:22:08 by recon-1.2 · entity Magic Unicorn Inc", conf: 0.97, mat: "M", agent: "recon-1.2" },
      { id: 10, title: "Daniel Tran · $420", body: "Posted 14:15:33 by bookkeeper · 1099-NEC tracked", conf: 0.99, mat: "L", agent: "bookkeeper" },
      { id: 11, title: "Mercury fees · $25", body: "Posted 13:48:19 by bookkeeper", conf: 0.99, mat: "L", agent: "bookkeeper" }
    ]
  }
] as const;

const toneMap: Record<string, string> = {
  pending: "border-pending/40 bg-pending/[0.04]",
  agent: "border-agent-500/40 bg-agent-500/[0.04]",
  credit: "border-credit/30 bg-credit/[0.03]"
};

const toneDotMap: Record<string, string> = {
  pending: "bg-pending",
  agent: "bg-agent-500",
  credit: "bg-credit"
};

export default function ReconciliationQueuePage() {
  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Reconciliation queue"
          subtitle="Where agent-proposed actions wait for you. Bulk-approve when n>1 selected."
          action={
            <div className="flex items-center gap-2 text-xs">
              <StatusChip tone="credit">47 auto-approved today</StatusChip>
              <StatusChip tone="pending">4 awaiting review</StatusChip>
            </div>
          }
        />

        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {COLUMNS.map((col) => (
            <div key={col.title}>
              <div className="mb-3 flex items-center gap-2">
                <span className={`h-1.5 w-1.5 rounded-full ${toneDotMap[col.tone]}`} />
                <h2 className="text-sm font-semibold">{col.title}</h2>
                <span className="ml-auto rounded-full bg-white/5 px-2 py-0.5 font-mono text-[10px] text-ink-tertiary">
                  {col.count}
                </span>
              </div>
              <div className="space-y-3">
                {col.items.map((item) => (
                  <Card
                    key={item.id}
                    className={`${toneMap[col.tone]} cursor-pointer transition hover:border-strong`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <h3 className="text-sm font-semibold">{item.title}</h3>
                      <ConfidenceDot value={item.conf} />
                    </div>
                    <p className="mt-2 text-xs leading-relaxed text-ink-secondary">{item.body}</p>
                    <div className="mt-3 flex items-center justify-between">
                      <div className="flex items-center gap-2 text-[11px] text-ink-tertiary">
                        <MaterialityBadge level={item.mat as "L" | "M" | "H"} />
                        <span className="font-mono">{item.agent}</span>
                      </div>
                      {col.tone === "credit" ? (
                        <ChevronRight size={14} className="text-ink-tertiary" />
                      ) : (
                        <div className="flex items-center gap-1">
                          <button
                            className="inline-flex h-7 items-center gap-1 rounded-md bg-credit/15 px-2 text-[11px] font-medium text-credit hover:bg-credit/25"
                            title="Approve"
                          >
                            <Check size={12} /> Approve
                          </button>
                          <button
                            className="inline-flex h-7 items-center justify-center rounded-md border border-subtle bg-surface px-2 text-ink-secondary hover:bg-elevated"
                            title="Reject"
                          >
                            <X size={12} />
                          </button>
                          <button
                            className="inline-flex h-7 items-center justify-center rounded-md border border-subtle bg-surface px-2 text-ink-secondary hover:bg-elevated"
                            title="Ask the agent"
                          >
                            <MessageCircle size={12} />
                          </button>
                        </div>
                      )}
                    </div>
                  </Card>
                ))}
              </div>
            </div>
          ))}
        </div>
      </PageShell>
    </AppShell>
  );
}

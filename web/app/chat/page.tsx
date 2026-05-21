import { ArrowUp, Bot, Paperclip, User } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { cn } from "@/lib/cn";

const MESSAGES = [
  {
    role: "agent" as const,
    agent: "Accounting-Ops",
    body: "Good afternoon. Here's where things stand on Magic Unicorn Inc as of right now:\n\n- Cash position: $487,193 across 6 accounts\n- 23 transactions auto-posted today\n- 3 items waiting for your review (one is the Mercury ACH I flagged earlier)\n- Q2 estimated tax projection: $11,200, due June 16\n\nAnything you want to dig into?"
  },
  {
    role: "user" as const,
    body: "What's the Mercury ACH thing?"
  },
  {
    role: "agent" as const,
    agent: "recon-1.2",
    body: "Inbound $5,000 ACH on May 19, account ending 2847. Looks like an inter-entity transfer from Magic Unicorn Inc to Majiks JV (the description says 'Majiks contributor float'), but I can't find a matching outbound debit in Magic Unicorn Inc's accounts.\n\nTwo possibilities:\n1. It's actually external revenue we haven't catalogued yet (royalty advance? customer payment?)\n2. The outbound side is mis-categorized in MU Inc — search history shows a $5,000 'Misc Outflow' on May 18 that might be it.\n\nWant me to dig into option 2 and propose a recategorization?"
  },
  {
    role: "user" as const,
    body: "Yeah, go ahead. If it's option 2, just queue the recategorization for my approval."
  },
  {
    role: "agent" as const,
    agent: "recon-1.2",
    body: "Working on it. Pulling the May 18 'Misc Outflow' detail and matching counterparty + amount + date. Will queue a proposal in Reconciliation if confident, or flag this back if it's actually external revenue.\n\nETA: about 12 seconds. I'll ping you when ready."
  }
];

export default function ChatPage() {
  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Chat with your agents"
          subtitle="Ask anything. The orchestrator routes to the right specialist. Every action returns through the approval queue."
          action={
            <div className="flex items-center gap-2 text-xs">
              <StatusChip tone="agent">3 agents online</StatusChip>
              <select className="rounded-md border border-default bg-surface px-2 py-1 text-xs">
                <option>Magic Unicorn Inc</option>
                <option>Aaron Stransky (personal)</option>
                <option>Majiks JV</option>
                <option>Consolidated</option>
              </select>
            </div>
          }
        />

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
          <Card padded={false} className="flex flex-col lg:col-span-3">
            <div className="flex-1 space-y-5 overflow-y-auto p-6">
              {MESSAGES.map((msg, i) => (
                <div
                  key={i}
                  className={cn("flex gap-3", msg.role === "user" && "flex-row-reverse")}
                >
                  <div
                    className={cn(
                      "flex h-7 w-7 shrink-0 items-center justify-center rounded-full ring-1 ring-inset",
                      msg.role === "agent"
                        ? "bg-elevated text-agent-500 ring-agent-500/30"
                        : "bg-brand-500/20 text-brand-400 ring-brand-500/40"
                    )}
                  >
                    {msg.role === "agent" ? <Bot size={14} /> : <User size={14} />}
                  </div>
                  <div
                    className={cn(
                      "max-w-[80%] space-y-1",
                      msg.role === "user" && "items-end text-right"
                    )}
                  >
                    {msg.role === "agent" && (
                      <p className="text-[11px] font-medium uppercase tracking-wider text-agent-500">
                        {msg.agent}
                      </p>
                    )}
                    <div
                      className={cn(
                        "whitespace-pre-line rounded-lg px-4 py-3 font-reasoning text-sm leading-relaxed",
                        msg.role === "agent"
                          ? "border-l-2 border-agent-500/60 bg-elevated text-ink-primary"
                          : "bg-brand-500/10 text-ink-primary"
                      )}
                    >
                      {msg.body}
                    </div>
                  </div>
                </div>
              ))}
            </div>

            <div className="border-t border-subtle p-4">
              <div className="relative">
                <textarea
                  rows={2}
                  placeholder="Ask the agents anything — try 'What's my burn this quarter?' or 'Categorize today's Stripe payouts'"
                  className="w-full resize-none rounded-lg border border-default bg-canvas px-4 py-3 pr-24 text-sm placeholder:text-ink-disabled focus:border-brand-500 focus:outline-none"
                />
                <div className="absolute right-2 top-2 flex items-center gap-1.5">
                  <button
                    className="inline-flex h-7 w-7 items-center justify-center rounded-md text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
                    title="Attach file"
                  >
                    <Paperclip size={14} />
                  </button>
                  <button className="inline-flex h-7 items-center gap-1 rounded-md bg-brand-500 px-2.5 text-[11px] font-medium text-white shadow-brand-glow hover:bg-brand-600">
                    <ArrowUp size={14} />
                  </button>
                </div>
              </div>
              <p className="mt-2 text-[10px] text-ink-tertiary">
                Runtime: Tier 1 local Qwen on midboy1. Escalates to Claude Opus 4.7 on Tier 3.
              </p>
            </div>
          </Card>

          <Card padded={false} className="lg:col-span-2">
            <div className="border-b border-subtle px-5 py-3">
              <h2 className="text-sm font-semibold">Agent workspace</h2>
              <p className="mt-0.5 text-[11px] text-ink-tertiary">
                Files and queries the agent is touching right now
              </p>
            </div>
            <div className="space-y-3 p-5 text-sm">
              <WorkspaceItem title="Reading transactions" body="MU Inc · May 17-19 · 64 rows" badge="Postgres" />
              <WorkspaceItem title="Matching counterparty" body="Mercury account 2847 -> MU Inc Operating" badge="FalkorDB graph" />
              <WorkspaceItem title="Drafting recategorization" body="Current: 9999·Misc Outflow · Proposed: 1500·Inter-entity transfer · MU->Majiks JV" badge="Proposal" tone="agent" />
              <WorkspaceItem title="Will queue if confident" body="Confidence threshold 0.85 · ETA 12s" badge="Awaiting" tone="pending" />
            </div>
          </Card>
        </div>
      </PageShell>
    </AppShell>
  );
}

function WorkspaceItem({
  title,
  body,
  badge,
  tone = "ink"
}: {
  title: string;
  body: string;
  badge: string;
  tone?: "agent" | "pending" | "ink";
}) {
  const map: Record<string, string> = {
    agent: "bg-agent-500/15 text-agent-500",
    pending: "bg-pending/15 text-pending",
    ink: "bg-white/5 text-ink-secondary"
  };
  return (
    <div className="rounded-md border border-subtle bg-canvas/40 p-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">{title}</h3>
        <span
          className={cn(
            "rounded-full px-2 py-0.5 text-[10px] uppercase tracking-wider",
            map[tone]
          )}
        >
          {badge}
        </span>
      </div>
      <p className="mt-1 font-mono text-[11px] text-ink-secondary">{body}</p>
    </div>
  );
}

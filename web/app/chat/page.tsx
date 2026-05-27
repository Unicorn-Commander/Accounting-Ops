import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { ChatComposer, ChatThread } from "@/components/agent-chat-thread";
import { getActiveContext } from "@/lib/active-context";
import { CHAT_MESSAGES } from "@/lib/agent-chat";
import { cn } from "@/lib/cn";

export default async function ChatPage() {
  const ctx = await getActiveContext();
  const canConsolidate = ctx.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;
  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Chat with your agents"
          subtitle="Ask anything. The orchestrator routes to the right specialist. Every action returns through the approval queue."
          action={
            <div className="flex items-center gap-2 text-xs">
              <StatusChip tone="agent">3 agents online</StatusChip>
              <EntitySelector
                entities={ctx.entities}
                activeEntityId={ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id}
                canConsolidate={canConsolidate}
                size="sm"
              />
            </div>
          }
        />

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
          <Card padded={false} className="flex flex-col lg:col-span-3">
            <ChatThread messages={CHAT_MESSAGES} className="flex-1" />
            <ChatComposer />
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

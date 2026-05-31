import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { ChatShell } from "@/components/chat-shell";
import { getActiveContext } from "@/lib/active-context";
import { fetchPersonas } from "@/lib/personas-fetch";
import { cn } from "@/lib/cn";

export default async function ChatPage() {
  const ctx = await getActiveContext();
  const personas = await fetchPersonas();
  const canConsolidate = ctx.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;
  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Chat with your agents"
          subtitle="Pick a persona and ask. Strategic agents synthesize. Specialists dig deep. Operational keeps the lights on. Every action returns through the approval queue."
          action={
            <div className="flex items-center gap-2 text-xs">
              <StatusChip tone="agent">
                {personas.brigade_online
                  ? "Brigade online"
                  : "Brigade offline"}
              </StatusChip>
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
            <ChatShell
              personas={personas.personas}
              brigadeOnline={personas.brigade_online}
              brigadeReason={personas.brigade_reason}
              initialSlug="cfo-agent"
            />
          </Card>

          <Card padded={false} className="lg:col-span-2">
            <div className="border-b border-subtle px-5 py-3">
              <h2 className="text-sm font-semibold">Agent workspace</h2>
              <p className="mt-0.5 text-[11px] text-ink-tertiary">
                Files and queries the agent is touching right now
              </p>
            </div>
            <div className="space-y-3 p-5 text-sm">
              <WorkspaceItem
                title="CFO Agent ready"
                body="Strategic synthesis across cash, runway, P&L, board narrative. Reaches for cross-app tools when Brigade is online."
                badge="Strategic"
                tone="agent"
              />
              <WorkspaceItem
                title={personas.brigade_online ? "Brigade catalog ready" : "Brigade unavailable"}
                body={
                  personas.brigade_online
                    ? "Cross-app tools (project.*, meeting.*, contact.*) are reachable for callers with brigade:cross_app scope."
                    : `Local tools only · ${personas.brigade_reason}`
                }
                badge={personas.brigade_online ? "Federated" : "Local"}
                tone={personas.brigade_online ? "agent" : "ink"}
              />
              <WorkspaceItem
                title="Persona registry"
                body={`${personas.personas.length} personas loaded across ${
                  new Set(personas.personas.map((p) => p.tier)).size
                } tiers.`}
                badge="Registry"
              />
              <WorkspaceItem
                title="LLM routing"
                body="CFO Agent: Claude Opus 4.7 by default (override via CFO_AGENT_MODEL). Falls back to Sonnet 4.5 when Opus is unreachable."
                badge="Runtime"
                tone="pending"
              />
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

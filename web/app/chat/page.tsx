import { Bot, Plus } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { ChatComposer, ChatThread } from "@/components/agent-chat-thread";
import { getActiveContext } from "@/lib/active-context";
import { CHAT_MESSAGES } from "@/lib/agent-chat";

export default async function ChatPage() {
  const ctx = await getActiveContext();
  const canConsolidate = ctx.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;

  // CHAT_MESSAGES is intentionally empty until the live chat endpoint ships.
  const messages = CHAT_MESSAGES;

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
          <Card padded={false} className="flex flex-col lg:col-span-3 min-h-[480px]">
            <div className="flex items-center justify-between border-b border-subtle px-5 py-3">
              <div className="flex items-center gap-2">
                <Bot size={14} className="text-agent-500" />
                <h2 className="text-sm font-semibold">New chat</h2>
              </div>
              <button
                type="button"
                aria-label="Start a new chat session"
                className="inline-flex items-center gap-1 rounded-md border border-default bg-surface px-2 py-1 text-[11px] text-ink-secondary hover:bg-elevated hover:text-ink-primary"
              >
                <Plus size={11} />
                New chat
              </button>
            </div>
            <ChatThread messages={messages} className="flex-1" />
            <ChatComposer />
          </Card>

          <Card padded={false} className="lg:col-span-2">
            <div className="border-b border-subtle px-5 py-3">
              <h2 className="text-sm font-semibold">Agent workspace</h2>
              <p className="mt-0.5 text-[11px] text-ink-tertiary">
                Files and queries the agent is touching right now
              </p>
            </div>
            <div className="px-5 py-8 text-center text-xs text-ink-tertiary">
              <p>Nothing in flight.</p>
              <p className="mt-1">
                When you start a chat, this panel shows the live tools, files,
                and database queries the agent is using.
              </p>
            </div>
          </Card>
        </div>
      </PageShell>
    </AppShell>
  );
}

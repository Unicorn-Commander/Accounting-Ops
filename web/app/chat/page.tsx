// /chat — Claude.ai-style in-app chat with the tax-agent team.
//
// Server Component:
//  - resolves active workspace + entity from cookies
//  - preloads the personas list (cached after first call) + this user's
//    chat session list for the sidebar
//  - hydrates ChatClient with a default persona (CPA Tax Resolver) and
//    a default tax year (current year - 1, i.e. the most-recent applicable
//    return)
//
// The Client Component handles all the streaming + state.

import Link from "next/link";
import { redirect } from "next/navigation";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { PageShell, SectionHeader } from "@/components/primitives";
import { ChatClient } from "@/components/chat/chat-client";
import { getActiveContext } from "@/lib/active-context";
import { fetchChatPersonas, fetchChatSessions } from "@/lib/chat";

export const dynamic = "force-dynamic";

export default async function ChatPage({
  searchParams
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const ctx = await getActiveContext();
  const sp = await searchParams;

  if (!ctx.activeWorkspace) {
    redirect("/welcome");
  }

  const canConsolidate = ["owner", "admin"].includes(ctx.activeWorkspace.role);

  // The chat is per-entity. Force the user out of consolidated mode for /chat:
  // tools that read journals require a single entity_id.
  const activeEntity = ctx.activeEntity ?? ctx.entities[0];
  if (!activeEntity) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader
            title="Chat with your agents"
            subtitle="No entity to chat about yet. Provision one in this workspace first."
          />
        </PageShell>
      </AppShell>
    );
  }

  const [personasRes, sessions] = await Promise.all([
    fetchChatPersonas(),
    fetchChatSessions(ctx.activeWorkspace.id, { limit: 50 })
  ]);

  const personas = personasRes?.personas ?? [];
  const defaultPersonaSlug = personasRes?.default ?? personas[0]?.slug ?? "cpa-tax-resolver";

  const requestedSession =
    typeof sp.session === "string" ? sp.session : null;
  const initialActiveSessionId =
    requestedSession && sessions.some((s) => s.id === requestedSession)
      ? requestedSession
      : null;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Chat with your agents"
          subtitle="Pick an agent, set the entity + year, ask. DRAFT memos route to the CPA queue; the Approvals queue is the only place anything actually changes."
          action={
            <div className="flex items-center gap-2 text-xs">
              <EntitySelector
                entities={ctx.entities}
                activeEntityId={activeEntity.id}
                canConsolidate={canConsolidate}
                showConsolidated={false}
                size="sm"
              />
            </div>
          }
        />

        {personas.length === 0 ? (
          <div className="rounded-md border border-pending/40 bg-pending/[0.05] p-4 text-sm text-pending">
            No agent personas registered on the backend yet. Check the API
            container logs or visit{" "}
            <Link href="/help" className="underline">
              /help
            </Link>
            .
          </div>
        ) : (
          <ChatClient
            workspaceId={ctx.activeWorkspace.id}
            entities={ctx.entities.map((e) => ({
              id: e.id,
              display_name: e.display_name,
              slug: e.slug
            }))}
            activeEntityId={activeEntity.id}
            defaultPersonaSlug={defaultPersonaSlug}
            personas={personas}
            initialSessions={sessions}
            initialActiveSessionId={initialActiveSessionId}
          />
        )}
      </PageShell>
    </AppShell>
  );
}

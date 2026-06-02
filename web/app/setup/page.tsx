// /setup — first-run conversational provisioning surface.
//
// Server Component:
//  - Resolves the active workspace from cookies.
//  - If 0 entities → full intake (Setup Assistant pre-bound).
//  - If N entities → "Add another entity" framing.
//  - Reads optional query params:
//      ?entity_kind=llc          — frames the conversation around adding an LLC
//      ?dba=true                  — frames as a DBA flow (no new entity row)
//      ?session=<sid>             — resume an existing setup session
//      ?from_chat=true            — entered from /chat, keep persona on Setup Assistant
//
// Hands the heavy lifting to SetupClient (a thin wrapper around ChatClient
// that locks the persona and listens for `mark_intake_complete`).

import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { PageShell, SectionHeader, Card } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { fetchChatPersonas, fetchChatSessions } from "@/lib/chat";
import { SetupClient } from "./setup-client";

export const dynamic = "force-dynamic";

export default async function SetupPage({
  searchParams
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const ctx = await getActiveContext();
  const sp = await searchParams;

  if (!ctx.activeWorkspace) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader
            title="Setup Assistant"
            subtitle="No workspace is provisioned for your account yet."
          />
          <Card>
            <p className="text-sm text-ink-secondary">
              You need a workspace before you can set up an entity. Talk to an
              admin to be invited, or visit{" "}
              <Link href="/help" className="text-brand-400 hover:underline">
                /help
              </Link>{" "}
              to get one created.
            </p>
          </Card>
        </PageShell>
      </AppShell>
    );
  }

  // Pre-fetch personas + sessions (the Setup Assistant is one of them).
  const [personasRes, sessions] = await Promise.all([
    fetchChatPersonas(),
    fetchChatSessions(ctx.activeWorkspace.id, { limit: 50 })
  ]);

  const personas = personasRes?.personas ?? [];
  const setupPersona = personas.find((p) => p.slug === "setup-assistant");

  if (!setupPersona) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader
            title="Setup Assistant"
            subtitle="Setup Assistant persona is not registered on this server."
          />
          <Card>
            <p className="text-sm text-debit">
              The backend doesn&apos;t expose a persona with slug{" "}
              <code className="font-mono">setup-assistant</code>. Check
              <code className="ml-1 font-mono">agents/chat/persona_registry.py</code>{" "}
              and restart the API container.
            </p>
          </Card>
        </PageShell>
      </AppShell>
    );
  }

  // Pick the entity to bind the chat to. The Setup Assistant needs SOMETHING
  // for the chat-session row's entity_id FK. If the workspace has no entities,
  // we bind to a synthetic entity_id later — but the chat-session API requires
  // a real entity FK. So when 0 entities exist, we redirect the user with an
  // explanatory header instead.
  //
  // To avoid the chicken-and-egg, the first thing the assistant does is call
  // create_entity_with_coa, and on success ALL subsequent calls bind to that
  // new entity. We resolve the binding at chat-create time using a placeholder.
  const hasEntities = ctx.entities.length > 0;
  const entityKindParam = typeof sp.entity_kind === "string" ? sp.entity_kind : null;
  const dbaParam = sp.dba === "true";
  const fromChat = sp.from_chat === "true";
  const requestedSession = typeof sp.session === "string" ? sp.session : null;
  const initialActiveSessionId =
    requestedSession && sessions.some((s) => s.id === requestedSession)
      ? requestedSession
      : null;

  // Compose the opening kickoff message the user will send first. The Setup
  // Assistant's system prompt already has its opening; this is the user's
  // implicit first turn (e.g. "I want to add an LLC").
  const kickoff = buildKickoff(hasEntities, entityKindParam, dbaParam);

  const subtitleParts: string[] = [];
  if (!hasEntities) {
    subtitleParts.push("Welcome — let's get your first entity set up.");
  } else if (dbaParam) {
    subtitleParts.push("Add a DBA (no new entity row, just a sub-account label).");
  } else if (entityKindParam) {
    subtitleParts.push(`Add a new ${entityKindParam.toUpperCase()} entity to ${ctx.activeWorkspace.display_name}.`);
  } else {
    subtitleParts.push(`Add another entity to ${ctx.activeWorkspace.display_name}.`);
  }
  const subtitle = subtitleParts.join(" ");

  // For chat session creation, we need an entity_id. If there are entities,
  // use the active one. If there are none, the SetupClient will call
  // /api/workspaces/{wid}/setup/bootstrap on mount to provision a "Setup
  // Sandbox" placeholder and rebind to its id. We pass null here as the
  // signal to the SetupClient that it needs to bootstrap.
  const bindingEntityId =
    ctx.activeEntity?.id ?? ctx.entities[0]?.id ?? null;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Setup Assistant"
          subtitle={subtitle}
        />

        <SetupClient
          workspaceId={ctx.activeWorkspace.id}
          workspaceName={ctx.activeWorkspace.display_name}
          entities={ctx.entities.map((e) => ({
            id: e.id,
            display_name: e.display_name,
            slug: e.slug
          }))}
          activeEntityId={bindingEntityId}
          personas={personas}
          initialSessions={sessions}
          initialActiveSessionId={initialActiveSessionId}
          kickoffMessage={kickoff}
          hasEntities={hasEntities}
          fromChat={fromChat}
        />
      </PageShell>
    </AppShell>
  );
}

function buildKickoff(
  hasEntities: boolean,
  entityKind: string | null,
  isDba: boolean
): string | null {
  if (isDba) {
    return "I want to add a DBA under one of my existing entities. Walk me through it.";
  }
  if (entityKind) {
    return `I want to add a new ${entityKind.toUpperCase()} entity. Walk me through it.`;
  }
  if (!hasEntities) {
    return null; // Let the assistant open with its standard greeting.
  }
  return "I want to add another entity to this workspace.";
}

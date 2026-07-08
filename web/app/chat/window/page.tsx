// /chat/window — the pop-out target. A MINIMAL chat surface with NO app
// nav shell (no SiteHeader, no sidebar, no breadcrumb), meant to be opened in
// its own browser window via window.open(...) from the ChatLauncher.
//
// It resumes the SAME conversation the in-page surface had via query params:
//   ?session=<sessionId>&entity=<entityId>
//
// - entity: pins the single entity the chat is bound to. We resolve it against
//   the caller's workspace entities; if the param is missing or stale we fall
//   back to the active (cookie) entity, then the first entity.
// - session: optional; when present EmbeddedChat loads that session's history
//   and continues it. When absent the window starts a fresh conversation
//   (created lazily on first send), defaulting to the CFO persona.
//
// Reuses the exact same chat engine (EmbeddedChat → useChatStream + Composer
// + MessageBubble + PersonaPicker) as the floating/sidebar surfaces.

import Link from "next/link";
import { EmbeddedChat } from "@/components/chat/embedded-chat";
import { getActiveContext } from "@/lib/active-context";
import { fetchChatPersonas } from "@/lib/chat";

export const dynamic = "force-dynamic";

export default async function ChatWindowPage({
  searchParams
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const ctx = await getActiveContext();
  const sp = await searchParams;

  const requestedSession =
    typeof sp.session === "string" ? sp.session : null;
  const requestedEntity = typeof sp.entity === "string" ? sp.entity : null;

  if (!ctx.activeWorkspace) {
    return (
      <WindowFrame title="Chat">
        <EmptyNotice>
          No workspace is provisioned for your account yet. Open{" "}
          <Link href="/help" className="text-brand-400 hover:underline">
            /help
          </Link>{" "}
          to get started.
        </EmptyNotice>
      </WindowFrame>
    );
  }

  // Resolve the single entity: prefer the ?entity= param when it points to a
  // real entity in this workspace, else the active/first entity.
  const entity =
    (requestedEntity &&
      ctx.entities.find((e) => e.id === requestedEntity)) ||
    ctx.activeEntity ||
    ctx.entities[0] ||
    null;

  if (!entity) {
    return (
      <WindowFrame title="Chat">
        <EmptyNotice>
          No entity to chat about yet. Provision one in this workspace first.
        </EmptyNotice>
      </WindowFrame>
    );
  }

  const personasRes = await fetchChatPersonas();
  const personas = personasRes?.personas ?? [];
  const defaultPersonaSlug =
    personasRes?.default ?? personas[0]?.slug ?? "cfo-agent";

  return (
    <WindowFrame title={entity.display_name}>
      {personas.length === 0 ? (
        <EmptyNotice>
          No agent personas are registered on the backend yet. Check the API
          container, then reopen this window.
        </EmptyNotice>
      ) : (
        <EmbeddedChat
          workspaceId={ctx.activeWorkspace.id}
          entityId={entity.id}
          entityName={entity.display_name}
          personas={personas}
          defaultPersonaSlug={defaultPersonaSlug}
          initialSessionId={requestedSession}
          className="flex-1 min-h-0"
        />
      )}
    </WindowFrame>
  );
}

// Minimal standalone frame — fills the popup window, dark theme inherited from
// the root <html class="dark">. A slim title row replaces the app shell.
function WindowFrame({
  title,
  children
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex h-screen min-h-0 flex-col bg-canvas">
      <header className="flex items-center gap-2 border-b border-subtle bg-elevated/40 px-3 py-2">
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full rounded-full bg-agent-500 opacity-75 animate-pulse-soft" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-agent-500" />
        </span>
        <p className="truncate text-xs font-semibold text-ink-primary">
          Chat · {title}
        </p>
        <Link
          href="/chat"
          className="ml-auto text-[10px] text-ink-tertiary hover:text-ink-secondary hover:underline"
          title="Open the full chat page in the main app"
        >
          Full chat ↗
        </Link>
      </header>
      {children}
    </div>
  );
}

function EmptyNotice({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <p className="max-w-sm text-center text-sm text-ink-secondary">{children}</p>
    </div>
  );
}

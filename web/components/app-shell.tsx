import { cookies } from "next/headers";
import { SiteHeader } from "@/components/site-header";
import { AppSidebar } from "@/components/app-sidebar";
import { BottomNav } from "@/components/bottom-nav";
import { ChatLauncher } from "@/components/chat/chat-launcher";
import { ContextBreadcrumb } from "@/components/context-breadcrumb";
import { GlobalToast } from "@/components/global-toast";
import { getActiveContext } from "@/lib/active-context";
import { fetchChatPersonas } from "@/lib/chat";
import { auth } from "@/lib/auth";

export async function AppShell({
  children,
  taxYear,
}: {
  children: React.ReactNode;
  /** When set, the breadcrumb appends a `Tax Year · N` segment. */
  taxYear?: number | string;
}) {
  const session = await auth();
  const ctx = session?.user ? await getActiveContext() : null;
  const canAdmin = ctx?.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;
  // Community Edition hides the enterprise (multi-client CPA) nav; the matching
  // routers are also unmounted server-side (see app/main.py).
  const isCommunity = (process.env.AO_EDITION ?? "enterprise").toLowerCase() === "community";

  const cookieStore = await cookies();
  const collapsed = cookieStore.get("aops_sidebar_collapsed")?.value === "1";

  // Resolve the single entity + personas for the global chat launcher. The
  // chat tools require ONE entity_id, so we pin the active (or first) entity.
  // Personas fetch is best-effort — the launcher simply hides if it 401s/500s.
  const chatEntity = ctx?.activeWorkspace
    ? ctx.activeEntity ?? ctx.entities[0] ?? null
    : null;
  const personasRes =
    ctx?.activeWorkspace && chatEntity ? await fetchChatPersonas() : null;
  const chatPersonas = personasRes?.personas ?? [];
  const chatDefaultPersona =
    personasRes?.default ?? chatPersonas[0]?.slug ?? "cfo-agent";

  return (
    <>
      <SiteHeader variant="app" />
      <div className="flex">
        <AppSidebar
          canAdmin={canAdmin}
          initialCollapsed={collapsed}
          workspaceName={ctx?.activeWorkspace?.display_name ?? "Workspace"}
          isCommunity={isCommunity}
        />
        <main className="min-w-0 flex-1 pb-24">
          {ctx?.activeWorkspace && (
            <div className="border-b border-subtle bg-canvas/40 px-4 py-2 md:px-6">
              <div className="mx-auto w-full max-w-7xl">
                <ContextBreadcrumb taxYear={taxYear} />
              </div>
            </div>
          )}
          {children}
        </main>
      </div>
      {ctx?.activeWorkspace && chatEntity && chatPersonas.length > 0 && (
        <ChatLauncher
          workspaceId={ctx.activeWorkspace.id}
          entityId={chatEntity.id}
          entityName={chatEntity.display_name}
          personas={chatPersonas}
          defaultPersonaSlug={chatDefaultPersona}
        />
      )}
      <GlobalToast />
      {ctx?.activeWorkspace && <BottomNav canAdmin={canAdmin} isCommunity={isCommunity} />}
    </>
  );
}

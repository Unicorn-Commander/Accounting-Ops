import { cookies } from "next/headers";
import { SiteHeader } from "@/components/site-header";
import { AppSidebar } from "@/components/app-sidebar";
import { AgentChatPopout } from "@/components/agent-chat-popout";
import { ContextBreadcrumb } from "@/components/context-breadcrumb";
import { GlobalToast } from "@/components/global-toast";
import { getActiveContext } from "@/lib/active-context";
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

  const cookieStore = await cookies();
  const collapsed = cookieStore.get("aops_sidebar_collapsed")?.value === "1";

  return (
    <>
      <SiteHeader variant="app" />
      <div className="flex">
        <AppSidebar canAdmin={canAdmin} initialCollapsed={collapsed} />
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
      <AgentChatPopout />
      <GlobalToast />
    </>
  );
}

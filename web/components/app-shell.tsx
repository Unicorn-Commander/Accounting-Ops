import { SiteHeader } from "@/components/site-header";
import { AgentChatPopout } from "@/components/agent-chat-popout";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <>
      <SiteHeader variant="app" />
      <main className="min-h-screen pb-24">{children}</main>
      <AgentChatPopout />
    </>
  );
}

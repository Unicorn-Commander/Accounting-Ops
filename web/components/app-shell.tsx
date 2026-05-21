import { SiteHeader } from "@/components/site-header";
import { ActivityTicker } from "@/components/activity-ticker";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <>
      <SiteHeader variant="app" />
      <main className="min-h-screen pb-32">{children}</main>
      <ActivityTicker />
    </>
  );
}

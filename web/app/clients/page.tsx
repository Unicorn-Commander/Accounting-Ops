import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { ClientsTable } from "@/components/clients-table";
import { RecentActivityRow } from "@/components/clients-recent-activity";
import { getClientsTriage, getCpaRecentActivity } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function ClientsPage() {
  const [triage, activity] = await Promise.all([
    getClientsTriage(),
    getCpaRecentActivity(30),
  ]);

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Clients"
          subtitle={
            triage?.persona === "cpa"
              ? "Cross-client triage — most-urgent work first."
              : triage?.persona === "platform_admin"
                ? "All workspaces in the platform — platform_admin view."
                : "Every workspace you belong to."
          }
        />

        {!triage ? (
          <Card>
            <p className="text-sm text-ink-tertiary">
              No workspaces found. Ask a workspace owner to invite you.
            </p>
          </Card>
        ) : (
          <div className="flex flex-col gap-6">
            <ClientsTable
              persona={triage.persona}
              workspaces={triage.workspaces}
            />

            <Card>
              <div className="mb-3">
                <h2 className="text-sm font-semibold">Recent CPA activity</h2>
                <p className="text-xs text-ink-tertiary">
                  Last {activity?.days ?? 30} days across your workspaces
                  {activity?.items.length
                    ? ` — ${activity.items.length} item${activity.items.length === 1 ? "" : "s"}`
                    : ""}
                </p>
              </div>
              {!activity || activity.items.length === 0 ? (
                <p className="text-sm text-ink-tertiary">No recent activity.</p>
              ) : (
                <ul className="divide-y divide-subtle">
                  {activity.items.slice(0, 20).map((it) => (
                    <RecentActivityRow key={it.id} item={it} />
                  ))}
                </ul>
              )}
            </Card>
          </div>
        )}
      </PageShell>
    </AppShell>
  );
}

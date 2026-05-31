import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { Card, EmptyState, PageShell, SectionHeader } from "@/components/primitives";
import { Users } from "lucide-react";
import { ClientsTable } from "@/components/clients-table";
import { getClientsDashboard, getCpaRecentActivity } from "@/lib/api";

export default async function ClientsPage() {
  const [dashboard, activity] = await Promise.all([
    getClientsDashboard(),
    getCpaRecentActivity(30),
  ]);

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Clients"
          subtitle={
            dashboard?.persona === "cpa"
              ? "Every customer workspace you've been assigned to as a CPA."
              : dashboard?.persona === "platform_admin"
                ? "All workspaces in the platform — platform_admin view."
                : "Every workspace you belong to."
          }
        />

        {!dashboard ? (
          // H9: empty state for CPAs with no assigned clients (also covers
          // founders with zero workspaces). Uses the permission-denied tone
          // since "ask the owner to invite you" is the actual recovery path.
          <EmptyState
            variant="permission-denied"
            icon={<Users size={28} className="text-ink-disabled" aria-hidden="true" />}
            title="No clients assigned yet."
            description="A workspace owner needs to invite you with the CPA or accountant role before assigned clients show up here."
          />
        ) : dashboard.workspaces.length === 0 ? (
          <EmptyState
            variant="no-data"
            icon={<Users size={28} className="text-ink-disabled" aria-hidden="true" />}
            title="No clients in this view yet."
            description={
              dashboard.persona === "cpa"
                ? "Once a workspace owner invites you to their books, the client will appear here with open items and review status."
                : "You belong to no workspaces. Ask an owner to invite you, or create your own from /setup."
            }
          />
        ) : (
          <div className="flex flex-col gap-6">
            <ClientsTable
              persona={dashboard.persona}
              workspaces={dashboard.workspaces}
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
                    <li
                      key={it.id}
                      className="flex flex-col gap-1 py-3 md:flex-row md:items-center md:gap-4"
                    >
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm">
                          <span className="font-medium">{it.tool}</span>{" "}
                          <span className="text-ink-tertiary">→</span>{" "}
                          <span className="text-ink-secondary">
                            {it.outcome}
                          </span>
                        </p>
                        <p className="truncate text-xs text-ink-tertiary">
                          <Link
                            href={`/dashboard`}
                            className="hover:text-ink-secondary"
                          >
                            {it.workspace_name}
                          </Link>{" "}
                          · {it.model_version || "anonymous agent"}
                        </p>
                      </div>
                      <span className="text-[11px] text-ink-tertiary">
                        {new Date(it.created_at).toLocaleString()}
                      </span>
                    </li>
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

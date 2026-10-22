import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { Card, EmptyState, PageShell, SectionHeader } from "@/components/primitives";
import { Inbox, Users } from "lucide-react";
import { ClientsTable } from "@/components/clients-table";
import { RecentActivityRow } from "@/components/clients-recent-activity";
import { AddClientButton } from "@/components/add-client-button";
import { getClientsTriage, getCpaRecentActivity, getPersona } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function ClientsPage() {
  const [triage, activity, persona] = await Promise.all([
    getClientsTriage(),
    getCpaRecentActivity(30),
    getPersona(),
  ]);

  // Whether to surface the "Add client" affordance. Driven by the cheap
  // persona check (not triage) so a CPA with ZERO clients yet can still
  // onboard their first one from the empty state.
  const canAddClients = persona?.can_add_clients ?? false;

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
          action={
            <div className="flex items-center gap-2">
              {triage && triage.workspaces.length > 0 ? (
                // The triage table shows cross-client COUNTS; the work inbox is
                // the companion surface that lists the actual items (stalest
                // first) so a CPA can act without switching workspaces.
                <Link
                  href="/clients/inbox"
                  className="inline-flex items-center gap-1.5 rounded-md bg-brand-500/15 px-3 py-1.5 text-sm font-medium text-brand-300 transition hover:bg-brand-500/25"
                >
                  <Inbox size={14} /> Work inbox
                </Link>
              ) : null}
              {canAddClients ? <AddClientButton /> : null}
            </div>
          }
        />

        {!triage ? (
          // H9: empty state for CPAs with no assigned clients (also covers
          // founders with zero workspaces). permission-denied tone since
          // "ask the owner to invite you" is the actual recovery path.
          <EmptyState
            variant="permission-denied"
            icon={<Users size={28} className="text-ink-disabled" aria-hidden="true" />}
            title="No clients yet."
            description={
              canAddClients
                ? "Add your first client to set up their books — you'll own the new workspace, and you can invite the business owner so they hold their own data too. (A workspace owner can also invite you directly.)"
                : "A workspace owner needs to invite you with the CPA or accountant role before assigned clients show up here."
            }
            primaryAction={canAddClients ? <AddClientButton /> : undefined}
          />
        ) : triage.workspaces.length === 0 ? (
          <EmptyState
            variant="no-data"
            icon={<Users size={28} className="text-ink-disabled" aria-hidden="true" />}
            title="No clients in this view yet."
            description={
              canAddClients
                ? "Add a client to set up their books — they'll appear here with open items and review status. (A workspace owner can also invite you to existing books.)"
                : triage.persona === "cpa"
                  ? "Once a workspace owner invites you to their books, the client will appear here with open items and review status."
                  : "You belong to no workspaces. Ask an owner to invite you, or create your own from /setup."
            }
            primaryAction={canAddClients ? <AddClientButton /> : undefined}
          />

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

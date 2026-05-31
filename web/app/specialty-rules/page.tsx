// /specialty-rules — per-tenant policy engine surface.
//
// Wired to live compliance_rule rows via GET
// /api/workspaces/{wid}/specialty-rules. Replaces the previous static
// RULES array + "Preview" badging per UX audit issue I16.
//
// Authoring new rules still routes through the agent proposal pipeline
// (the Compliance agent drafts; a human approves) so the audit trail is
// preserved. Deactivate / reactivate are wired here directly.

import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { fetchSpecialtyRules } from "@/lib/specialty-rules";
import { SpecialtyRulesView } from "./specialty-rules-view";

export const dynamic = "force-dynamic";

export default async function SpecialtyRulesPage({
  searchParams
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const sp = await searchParams;
  const includeInactive =
    typeof sp.include_inactive === "string"
      ? sp.include_inactive === "true"
      : false;

  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;

  if (!workspace) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader
            title="Specialty rules"
            subtitle="Per-tenant policy engine."
          />
          <Card>
            <p className="text-sm text-ink-secondary">
              No workspace selected. Visit{" "}
              <Link
                href="/dashboard"
                className="text-brand-400 underline-offset-2 hover:underline"
              >
                Dashboard
              </Link>{" "}
              to pick one.
            </p>
          </Card>
        </PageShell>
      </AppShell>
    );
  }

  const initial = await fetchSpecialtyRules(workspace.id, {
    includeInactive
  });

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Specialty rules"
          subtitle="Per-tenant policy engine. Vertical compliance lives here. Each rule has temporal validity and an auditable change history."
        />

        <SpecialtyRulesView
          workspaceId={workspace.id}
          initial={initial}
          includeInactive={includeInactive}
          role={workspace.role}
        />
      </PageShell>
    </AppShell>
  );
}

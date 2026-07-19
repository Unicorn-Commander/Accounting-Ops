import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { EngagementClient } from "./engagement-client";

export const dynamic = "force-dynamic";

export default async function EngagementSettingsPage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;

  // Owners + admins may CHANGE the policy (the backend POST has an `admin`
  // role floor). Everyone else lands here read-only so they can see — but
  // not flip — the professional-review gate and submission channels.
  const canAdmin = workspace
    ? ["owner", "admin"].includes(workspace.role)
    : false;

  if (!workspace) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader
            title="Engagement & filing policy"
            subtitle="Control the professional-review gate and which submission channels are enabled"
          />
          <Card>
            <p className="text-sm text-ink-secondary">
              No active workspace. Pick or provision one from the workspace
              switcher to manage its engagement policy.
            </p>
          </Card>
        </PageShell>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Engagement & filing policy"
          subtitle="Control the professional-review gate and which submission channels are enabled"
        />
        <EngagementClient workspaceId={workspace.id} canAdmin={canAdmin} />
      </PageShell>
    </AppShell>
  );
}

import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { getByokStatus, type ByokStatus } from "@/lib/inference-settings";
import { InferenceSettingsClient } from "./page-client";

// Owner/admin only — matches the backend role floor (POST=admin, DELETE=owner).
const ADMIN_ROLES = ["owner", "client_owner", "admin", "platform_admin"];

export default async function InferenceSettingsPage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const canAdmin = workspace ? ADMIN_ROLES.includes(workspace.role) : false;
  // Only an owner (or platform_admin) may remove a key (backend DELETE floor).
  const canRemove =
    workspace !== null &&
    ["owner", "client_owner", "platform_admin"].includes(workspace.role);

  // Only fetch status when the caller is admin+ — viewers never see this page.
  const status: ByokStatus | null =
    workspace && canAdmin ? await getByokStatus(workspace.id) : null;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Inference & billing"
          subtitle="Choose how Accounting-Ops pays for AI. Use platform credits, or bring your own provider key and let the provider bill you directly."
        />

        {!workspace ? (
          <Card>
            <p className="text-sm text-ink-tertiary">No active workspace.</p>
          </Card>
        ) : !canAdmin ? (
          <Card>
            <p className="text-sm text-ink-tertiary">
              Owner or admin access is required to manage inference settings.
              Ask the workspace owner to update billing.
            </p>
          </Card>
        ) : (
          <InferenceSettingsClient
            workspaceId={workspace.id}
            workspaceName={workspace.display_name}
            initialStatus={
              status ?? {
                configured: false,
                provider: null,
                key_last4: null,
                base_url: null,
              }
            }
            canRemove={canRemove}
          />
        )}
      </PageShell>
    </AppShell>
  );
}

import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { getBillingStatus, type BillingStatus } from "@/lib/billing";
import { BillingClient } from "./page-client";

// Admin+ only — mirrors /settings/inference. Billing is an owner/admin surface.
const ADMIN_ROLES = ["owner", "client_owner", "admin", "platform_admin"];

export default async function BillingSettingsPage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const canAdmin = workspace ? ADMIN_ROLES.includes(workspace.role) : false;
  // Read AO_EDITION server-side exactly like AppShell. Community Edition has no
  // hosted credit plan — the page renders a BYOK-only pointer instead.
  const isCommunity =
    (process.env.AO_EDITION ?? "enterprise").toLowerCase() === "community";

  // Only fetch status when the caller is admin+ (viewers never see this page).
  // Null (endpoint not deployed / unreachable) degrades to a BYOK-only view.
  const status: BillingStatus | null =
    workspace && canAdmin ? await getBillingStatus(workspace.id) : null;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Billing & Credits"
          subtitle="Turn on the smart AI advisors for this workspace — with Unicorn Commander credits or your own provider key. Free bookkeeping never uses credits."
        />

        {!workspace ? (
          <Card>
            <p className="text-sm text-ink-tertiary">No active workspace.</p>
          </Card>
        ) : !canAdmin ? (
          <Card>
            <p className="text-sm text-ink-tertiary">
              Owner or admin access is required to manage billing. Ask the
              workspace owner to add credits or bring an API key.
            </p>
          </Card>
        ) : (
          <BillingClient
            workspaceName={workspace.display_name}
            isCommunity={isCommunity}
            status={status}
          />
        )}
      </PageShell>
    </AppShell>
  );
}

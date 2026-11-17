// Stripe connector settings: connection status plus the blocked-item panel.
// Server component: reads status + blocked-item state with the user's bearer;
// the panel's Resolve / Reopen forms render for owner/admin only (the backend
// enforces the same floor plus an interactive human session).

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { StripeStatusCard } from "@/components/stripe-status-card";
import { StripeBlockedItemPanel } from "@/components/stripe-blocked-item-panel";
import { getActiveContext } from "@/lib/active-context";
import { canManageBlockedItem } from "@/lib/stripe-blocked-item";
import { getStripeBlockedItem, getStripeStatus } from "@/lib/stripe-connector";

export default async function StripeConnectorSettingsPage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const [status, blocked] = workspace
    ? await Promise.all([getStripeStatus(workspace.id), getStripeBlockedItem(workspace.id)])
    : [null, null];
  const defaultEntityId =
    ctx.entities.find((e) => e.kind !== "personal")?.id ?? ctx.entities[0]?.id ?? null;

  return (
    <AppShell>
      <PageShell>
        <Link href="/connectors" className="mb-3 inline-flex items-center gap-1 text-xs text-ink-tertiary hover:text-ink-secondary">
          <ArrowLeft size={12} /> Connectors
        </Link>
        <SectionHeader
          title="Stripe"
          subtitle={workspace ? `Stripe connector settings for ${workspace.display_name}.` : "Stripe connector settings."}
        />
        {!workspace ? (
          <Card>
            <p className="text-sm text-ink-tertiary">No active workspace. Pick one from the switcher above.</p>
          </Card>
        ) : (
          <div className="flex flex-col gap-6">
            <Card>
              <h2 className="text-sm font-semibold">Connection</h2>
              <StripeStatusCard
                status={status}
                workspaceId={workspace.id}
                workspaceName={workspace.display_name}
                entityId={ctx.activeEntity?.id ?? null}
                defaultEntityId={defaultEntityId}
              />
            </Card>
            <Card>
              <StripeBlockedItemPanel
                workspaceId={workspace.id}
                initialView={blocked}
                canManage={canManageBlockedItem(workspace.role)}
              />
            </Card>
          </div>
        )}
      </PageShell>
    </AppShell>
  );
}

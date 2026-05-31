import Link from "next/link";
import { ExternalLink, HelpCircle } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import {
  Card,
  EmptyState,
  PageShell,
  SectionHeader,
  StatusChip
} from "@/components/primitives";
import { CheckCircle2 } from "lucide-react";
import { getActiveContext } from "@/lib/active-context";
import { getAccountMappings } from "@/lib/account-mappings";
import { formatUSD } from "@/lib/cash-position";
import { getRecentTransactions, type LiveTransaction } from "@/lib/transactions";
import { QueueColumnClient } from "@/components/queue-column";

export default async function ReconciliationQueuePage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const mappings = workspace ? await getAccountMappings(workspace.id) : null;
  const txnData = workspace ? await getRecentTransactions(workspace.id, 90, 500) : null;
  const transactions: LiveTransaction[] = txnData?.transactions ?? [];

  const needsReview = transactions.filter(
    (t) =>
      // Vendor rule matched but sign disagreed (e.g. Stripe but txn is negative)
      t.category_sign_matched === false ||
      // Account isn't mapped to an entity yet
      (t.entity_id == null && t.category_status !== "unknown")
  );

  const agentSuggested = transactions.filter(
    (t) => t.category_status === "auto" && t.entity_id != null
  );

  const unknown = transactions.filter((t) => t.category_status === "unknown");

  const unmappedAcctCount = mappings?.unmapped_count ?? 0;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Reconciliation queue"
          subtitle={
            workspace
              ? `${needsReview.length} need review · ${agentSuggested.length} agent-categorized · ${unknown.length} uncategorized`
              : "Where agent-proposed actions wait for you."
          }
          action={
            <div className="flex items-center gap-2 text-xs">
              {agentSuggested.length > 0 && (
                <StatusChip tone="credit">{agentSuggested.length} auto-categorized</StatusChip>
              )}
              {needsReview.length > 0 && (
                <StatusChip tone="pending">{needsReview.length} awaiting review</StatusChip>
              )}
            </div>
          }
        />

        {unmappedAcctCount > 0 && (
          <div className="mb-6 rounded-lg border border-pending/40 bg-pending/[0.06] px-5 py-4">
            <div className="flex items-start gap-3">
              <HelpCircle size={18} className="mt-0.5 shrink-0 text-pending" />
              <div className="flex-1">
                <p className="text-sm font-medium">
                  {unmappedAcctCount} {unmappedAcctCount === 1 ? "account" : "accounts"} unmapped
                </p>
                <p className="mt-1 text-xs text-ink-secondary">
                  Map each connected account to one of your entities so transactions land in the
                  right books. The entity column and entity selector both depend on this.
                </p>
              </div>
              <Link
                href="/connectors"
                className="inline-flex items-center gap-1.5 rounded-md border border-pending/50 bg-surface px-3 py-1.5 text-xs text-pending hover:bg-pending/10"
              >
                Map accounts <ExternalLink size={11} />
              </Link>
            </div>
          </div>
        )}

        {transactions.length === 0 ? (
          // H9: standardised empty state. "No items to reconcile" with a link
          // back to /approvals (which is where the daily reconciliation work
          // begins for users with active flows).
          <EmptyState
            variant="not-connected"
            title="No items to reconcile."
            description="The last 90 days are clean. Connect a bank or wait for the next sync; new imports land in the approvals queue first."
            primaryAction={
              <Link
                href="/connectors"
                className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600"
              >
                Connect a bank
              </Link>
            }
            secondaryAction={
              <Link
                href="/approvals"
                className="inline-flex items-center gap-1.5 rounded-md border border-default bg-surface px-3 py-1.5 text-xs text-ink-secondary hover:bg-elevated"
              >
                Open approvals
              </Link>
            }
          />
        ) : needsReview.length === 0 &&
          unknown.length === 0 &&
          agentSuggested.length === 0 ? (
          // H9: caught-up state — everything reconciled or auto-handled.
          <EmptyState
            variant="no-data"
            icon={<CheckCircle2 size={28} className="text-credit" aria-hidden="true" />}
            title="All caught up."
            description="No items need review. New imports show up here and split into the three columns automatically."
            secondaryAction={
              <Link
                href="/approvals"
                className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary"
              >
                Go to approvals
              </Link>
            }
          />
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <QueueColumnClient
              title="Needs review"
              tone="pending"
              items={needsReview.slice(0, 25)}
              total={needsReview.length}
              kind="review"
              workspaceId={workspace!.id}
            />
            <QueueColumnClient
              title="Uncategorized"
              tone="agent"
              items={unknown.slice(0, 25)}
              total={unknown.length}
              kind="unknown"
              workspaceId={workspace!.id}
            />
            <QueueColumnClient
              title="Auto-categorized"
              tone="credit"
              items={agentSuggested.slice(0, 25)}
              total={agentSuggested.length}
              kind="auto"
              workspaceId={workspace!.id}
            />
          </div>
        )}
      </PageShell>
    </AppShell>
  );
}



import Link from "next/link";
import { ExternalLink, HelpCircle } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import {
  Card,
  PageShell,
  SectionHeader,
  StatusChip
} from "@/components/primitives";
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
          <div className="mb-6 rounded-xl border border-pending/40 bg-pending/[0.06] px-5 py-4">
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
          <Card>
            <p className="text-sm text-ink-tertiary">
              No transactions in the last 90 days. Connect a bank or wait for the next sync from{" "}
              <Link href="/connectors" className="text-brand-400 underline-offset-2 hover:underline">
                Connectors
              </Link>
              .
            </p>
          </Card>
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



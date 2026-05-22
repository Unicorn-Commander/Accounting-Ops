import Link from "next/link";
import { ExternalLink, HelpCircle } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import {
  Card,
  MaterialityBadge,
  PageShell,
  SectionHeader,
  StatusChip
} from "@/components/primitives";
import { QueueRowActions } from "@/components/queue-row-actions";
import { getActiveContext } from "@/lib/active-context";
import { getAccountMappings } from "@/lib/account-mappings";
import { formatUSD } from "@/lib/cash-position";
import { getRecentTransactions, shortDate, type LiveTransaction } from "@/lib/transactions";
import { cn } from "@/lib/cn";

type QueueTone = "pending" | "agent" | "credit";

const toneMap: Record<QueueTone, string> = {
  pending: "border-pending/40 bg-pending/[0.04]",
  agent: "border-agent-500/40 bg-agent-500/[0.04]",
  credit: "border-credit/30 bg-credit/[0.03]"
};

const toneDotMap: Record<QueueTone, string> = {
  pending: "bg-pending",
  agent: "bg-agent-500",
  credit: "bg-credit"
};

function materialityFor(amountStr: string): "L" | "M" | "H" {
  const abs = Math.abs(parseFloat(amountStr));
  if (abs >= 1000) return "H";
  if (abs >= 75) return "M";
  return "L";
}

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
            <QueueColumn
              title="Needs review"
              tone="pending"
              items={needsReview.slice(0, 25)}
              total={needsReview.length}
              kind="review"
              workspaceId={workspace!.id}
            />
            <QueueColumn
              title="Uncategorized"
              tone="agent"
              items={unknown.slice(0, 25)}
              total={unknown.length}
              kind="unknown"
              workspaceId={workspace!.id}
            />
            <QueueColumn
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

function QueueColumn({
  title,
  tone,
  items,
  total,
  kind,
  workspaceId
}: {
  title: string;
  tone: QueueTone;
  items: LiveTransaction[];
  total: number;
  kind: "review" | "unknown" | "auto";
  workspaceId: string;
}) {
  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <span className={`h-1.5 w-1.5 rounded-full ${toneDotMap[tone]}`} />
        <h2 className="text-sm font-semibold">{title}</h2>
        <span className="ml-auto rounded-full bg-white/5 px-2 py-0.5 font-mono text-[10px] text-ink-tertiary">
          {items.length < total ? `${items.length}/${total}` : total}
        </span>
      </div>
      <div className="space-y-3">
        {items.length === 0 ? (
          <Card className="border-dashed">
            <p className="text-xs text-ink-tertiary">
              {kind === "review" && "Nothing needs your attention right now."}
              {kind === "unknown" && "Every transaction matched a vendor rule."}
              {kind === "auto" && "No auto-categorized transactions yet."}
            </p>
          </Card>
        ) : (
          items.map((t) => {
            const amt = parseFloat(t.amount);
            const isCredit = amt >= 0;
            const mat = materialityFor(t.amount);
            return (
              <Card
                key={t.id}
                className={`${toneMap[tone]} cursor-pointer transition hover:border-strong`}
              >
                <div className="flex items-start justify-between gap-2">
                  <h3 className="max-w-[200px] truncate text-sm font-semibold">
                    {t.payee ?? t.description ?? "—"}
                  </h3>
                  <span
                    className={cn(
                      "shrink-0 font-mono text-sm tabular-nums",
                      isCredit ? "text-credit" : "text-ink-primary"
                    )}
                  >
                    {isCredit ? "+" : "-"}
                    {formatUSD(Math.abs(amt))}
                  </span>
                </div>
                <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-ink-secondary">
                  {t.org_name} · {t.account_name} · {shortDate(t.posted_at)}
                </p>
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  {t.category && t.category !== "Uncategorized" && (
                    <span
                      className={cn(
                        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium",
                        t.category_sign_matched === false
                          ? "bg-pending/15 text-pending"
                          : "bg-brand-500/10 text-brand-300"
                      )}
                    >
                      {t.category}
                    </span>
                  )}
                  {t.entity_display_name && (
                    <span className="inline-flex items-center rounded-full bg-ink-tertiary/10 px-2 py-0.5 text-[10px] text-ink-secondary">
                      {t.entity_display_name}
                    </span>
                  )}
                  {!t.entity_display_name && (
                    <span className="inline-flex items-center rounded-full bg-pending/10 px-2 py-0.5 text-[10px] text-pending">
                      Unmapped
                    </span>
                  )}
                </div>
                <div className="mt-3 flex items-center justify-between">
                  <div className="flex items-center gap-2 text-[11px] text-ink-tertiary">
                    <MaterialityBadge level={mat} />
                    {t.category_sign_matched === false && (
                      <span className="text-pending">sign mismatch</span>
                    )}
                  </div>
                  <QueueRowActions
                    workspaceId={workspaceId}
                    transaction={t}
                    kind={kind}
                  />
                </div>
              </Card>
            );
          })
        )}
      </div>
    </div>
  );
}

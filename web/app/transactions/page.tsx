import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { LiveTransactionsTable } from "@/components/transactions-table";
import { PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { getRecentTransactions } from "@/lib/transactions";

export default async function TransactionsPage() {
  const ctx = await getActiveContext();
  const canConsolidate = ctx.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;
  const entityFilter =
    ctx.activeEntity?.id && !ctx.isConsolidated ? ctx.activeEntity.id : undefined;
  const txnData = ctx.activeWorkspace
    ? await getRecentTransactions(ctx.activeWorkspace.id, 90, 500, entityFilter)
    : null;

  const subtitle = ctx.activeWorkspace
    ? `Every money event across all connected accounts. Viewing ${
        ctx.isConsolidated ? "all entities (consolidated)" : ctx.activeEntity?.display_name ?? "—"
      }.`
    : "Every money event across all connected accounts.";

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Transactions"
          subtitle={subtitle}
          action={
            <EntitySelector
              entities={ctx.entities}
              activeEntityId={ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id}
              canConsolidate={canConsolidate}
              size="sm"
            />
          }
        />

        {/*
         * H9: removed the "Map accounts to entities" secondary action that
         * was previously passed as emptyAction. That CTA confused the
         * not-connected case (where mapping doesn't yet apply) — the
         * configured-but-empty case still surfaces it via the page-level
         * unmapped-accounts banner on /reconciliation-queue.
         */}
        <LiveTransactionsTable
          transactions={txnData?.transactions ?? []}
          daysWindow={txnData?.days_window ?? 90}
          totalCount={txnData?.total_count ?? 0}
          configured={txnData?.configured ?? false}
        />

        {txnData?.errors && txnData.errors.length > 0 && (
          <p className="mt-4 text-xs text-debit">
            Aggregator reported: {txnData.errors.join("; ")}
          </p>
        )}
      </PageShell>
    </AppShell>
  );
}

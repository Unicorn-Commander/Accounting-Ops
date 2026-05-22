import Link from "next/link";
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

        <LiveTransactionsTable
          transactions={txnData?.transactions ?? []}
          daysWindow={txnData?.days_window ?? 90}
          totalCount={txnData?.total_count ?? 0}
          configured={txnData?.configured ?? false}
          emptyAction={
            <div className="mt-4">
              <Link
                href="/connectors"
                className="text-ink-secondary underline-offset-2 hover:text-ink-primary hover:underline"
              >
                Map accounts to entities →
              </Link>
            </div>
          }
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

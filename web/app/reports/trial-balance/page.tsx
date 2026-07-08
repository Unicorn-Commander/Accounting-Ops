import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { getTrialBalance } from "@/lib/reports-trial-balance";
import { TrialBalanceView } from "./trial-balance-view";

type SearchParams = {
  as_of?: string;
};

function lastDayOfCurrentQuarter(now = new Date()): string {
  const year = now.getFullYear();
  const quarter = Math.floor(now.getMonth() / 3);
  const endMonth = quarter * 3 + 3;
  return new Date(year, endMonth, 0).toISOString().slice(0, 10);
}

export default async function TrialBalancePage(props: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await props.searchParams;
  const asOf = sp.as_of ?? lastDayOfCurrentQuarter();
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const canConsolidate = workspace
    ? ["owner", "admin"].includes(workspace.role)
    : false;
  const entityFilter = ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id;
  const data = workspace
    ? await getTrialBalance(workspace.id, asOf, entityFilter)
    : null;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Trial Balance"
          subtitle="As-of-date debit/credit per account"
          action={
            ctx.entities.length > 1 ? (
              <EntitySelector
                entities={ctx.entities}
                activeEntityId={
                  ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id
                }
                canConsolidate={canConsolidate}
                size="sm"
              />
            ) : null
          }
        />

        <TrialBalanceView asOf={asOf} data={data} />
      </PageShell>
    </AppShell>
  );
}

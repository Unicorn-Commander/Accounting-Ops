// Route-level loading skeleton for /transactions. Tier-2 audit H10.

import { AppShell } from "@/components/app-shell";
import { PageShell, RouteSkeleton } from "@/components/primitives";

export default function TransactionsLoading() {
  return (
    <AppShell>
      <PageShell>
        <RouteSkeleton variant="table" label="Loading transactions" />
      </PageShell>
    </AppShell>
  );
}

// Route-level loading skeleton for /reconciliation-queue. Tier-2 audit H10.

import { AppShell } from "@/components/app-shell";
import { PageShell, RouteSkeleton } from "@/components/primitives";

export default function ReconciliationLoading() {
  return (
    <AppShell>
      <PageShell>
        <RouteSkeleton variant="default" label="Loading reconciliation queue" />
      </PageShell>
    </AppShell>
  );
}

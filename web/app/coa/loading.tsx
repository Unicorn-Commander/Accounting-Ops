// Route-level loading skeleton for /coa. Tier-2 audit H10.

import { AppShell } from "@/components/app-shell";
import { PageShell, RouteSkeleton } from "@/components/primitives";

export default function CoaLoading() {
  return (
    <AppShell>
      <PageShell>
        <RouteSkeleton variant="coa" label="Loading chart of accounts" />
      </PageShell>
    </AppShell>
  );
}

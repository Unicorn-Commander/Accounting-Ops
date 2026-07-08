// Route-level loading skeleton for /taxes index. Tier-2 audit H10.

import { AppShell } from "@/components/app-shell";
import { PageShell, RouteSkeleton } from "@/components/primitives";

export default function TaxesLoading() {
  return (
    <AppShell>
      <PageShell>
        <RouteSkeleton variant="default" label="Loading taxes" />
      </PageShell>
    </AppShell>
  );
}

// Route-level loading skeleton for /clients. Tier-2 audit H10.

import { AppShell } from "@/components/app-shell";
import { PageShell, RouteSkeleton } from "@/components/primitives";

export default function ClientsLoading() {
  return (
    <AppShell>
      <PageShell>
        <RouteSkeleton variant="table" label="Loading clients" />
      </PageShell>
    </AppShell>
  );
}

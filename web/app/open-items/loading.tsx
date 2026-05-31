// Route-level loading skeleton for /open-items. Tier-2 audit H10.

import { AppShell } from "@/components/app-shell";
import { PageShell, RouteSkeleton } from "@/components/primitives";

export default function OpenItemsLoading() {
  return (
    <AppShell>
      <PageShell>
        <RouteSkeleton variant="default" label="Loading open items" />
      </PageShell>
    </AppShell>
  );
}

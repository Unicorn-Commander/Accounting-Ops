// Route-level loading skeleton for /clients/inbox. Mirrors clients/loading.tsx.

import { AppShell } from "@/components/app-shell";
import { PageShell, RouteSkeleton } from "@/components/primitives";

export default function WorkInboxLoading() {
  return (
    <AppShell>
      <PageShell>
        <RouteSkeleton variant="table" label="Loading work inbox" />
      </PageShell>
    </AppShell>
  );
}

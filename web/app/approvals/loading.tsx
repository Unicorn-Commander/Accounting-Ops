// Route-level loading skeleton for /approvals. Mirrors the cluster-review +
// table layout to avoid a blank screen during data fetch. Tier-2 audit H10.

import { AppShell } from "@/components/app-shell";
import { PageShell, RouteSkeleton } from "@/components/primitives";

export default function ApprovalsLoading() {
  return (
    <AppShell>
      <PageShell>
        <RouteSkeleton variant="table" label="Loading approvals" />
      </PageShell>
    </AppShell>
  );
}

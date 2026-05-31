// Route-level loading skeleton for /reports. Tier-2 audit H10.

import { AppShell } from "@/components/app-shell";
import { PageShell, RouteSkeleton } from "@/components/primitives";

export default function ReportsLoading() {
  return (
    <AppShell>
      <PageShell>
        <RouteSkeleton variant="reports" label="Loading reports" />
      </PageShell>
    </AppShell>
  );
}

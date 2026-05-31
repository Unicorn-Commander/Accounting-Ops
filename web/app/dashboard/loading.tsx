// Route-level loading skeleton for /dashboard. Shows the same 4-up KPI grid
// + main/side-rail layout the populated page uses, so first paint feels stable.
// Tier-2 audit H10.

import { AppShell } from "@/components/app-shell";
import { PageShell, RouteSkeleton } from "@/components/primitives";

export default function DashboardLoading() {
  return (
    <AppShell>
      <PageShell>
        <RouteSkeleton variant="dashboard" label="Loading dashboard" />
      </PageShell>
    </AppShell>
  );
}

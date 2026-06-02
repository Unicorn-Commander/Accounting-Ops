// Route-level loading skeleton for /invoices.

import { AppShell } from "@/components/app-shell";
import { PageShell, RouteSkeleton } from "@/components/primitives";

export default function InvoicesLoading() {
  return (
    <AppShell>
      <PageShell>
        <RouteSkeleton variant="table" label="Loading invoices" />
      </PageShell>
    </AppShell>
  );
}

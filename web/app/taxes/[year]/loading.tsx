// Skeleton shown while /taxes/[year] loads its server data.
// Visible during the await on fetch1120Package + fetchTaxReviewQueue +
// fetchSpecialistRuns. Tier-2 audit H10 — uses the shared narrative variant.

import { AppShell } from "@/components/app-shell";
import { PageShell, RouteSkeleton } from "@/components/primitives";

export default function Loading() {
  return (
    <AppShell>
      <PageShell>
        <RouteSkeleton variant="narrative" label="Loading tax year" />
      </PageShell>
    </AppShell>
  );
}

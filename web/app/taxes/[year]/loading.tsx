// Skeleton shown while /taxes/[year] loads its server data.
// Visible during the await on fetch1120Package + fetchTaxReviewQueue +
// fetchSpecialistRuns. Prevents the "white flash" between pages.

import { AppShell } from "@/components/app-shell";
import { Card, PageShell } from "@/components/primitives";

export default function Loading() {
  return (
    <AppShell>
      <PageShell>
        {/* Back-link placeholder */}
        <div className="mb-4 h-3 w-32 rounded bg-elevated animate-pulse" />

        {/* Header */}
        <div className="mb-6 flex items-end justify-between">
          <div className="space-y-2">
            <div className="h-7 w-64 rounded bg-elevated animate-pulse" />
            <div className="h-3.5 w-96 max-w-full rounded bg-elevated/60 animate-pulse" />
          </div>
          <div className="h-8 w-40 rounded-md bg-elevated animate-pulse" />
        </div>

        {/* Tab bar placeholder */}
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <div className="flex gap-1.5">
            {[1, 2, 3, 4, 5].map((i) => (
              <div
                key={i}
                className="h-7 w-24 rounded-md bg-elevated animate-pulse"
              />
            ))}
          </div>
          <div className="h-9 w-48 rounded-md bg-elevated animate-pulse" />
        </div>

        {/* Body skeleton */}
        <Card>
          <div className="space-y-4 animate-pulse">
            <div className="h-6 w-3/4 rounded bg-elevated" />
            <div className="space-y-2">
              <div className="h-3.5 w-full rounded bg-elevated/60" />
              <div className="h-3.5 w-11/12 rounded bg-elevated/60" />
              <div className="h-3.5 w-9/12 rounded bg-elevated/60" />
              <div className="h-3.5 w-10/12 rounded bg-elevated/60" />
            </div>
            <div className="h-5 w-1/2 rounded bg-elevated mt-6" />
            <div className="space-y-2">
              <div className="h-3.5 w-full rounded bg-elevated/60" />
              <div className="h-3.5 w-10/12 rounded bg-elevated/60" />
              <div className="h-3.5 w-11/12 rounded bg-elevated/60" />
            </div>
          </div>
        </Card>
      </PageShell>
    </AppShell>
  );
}

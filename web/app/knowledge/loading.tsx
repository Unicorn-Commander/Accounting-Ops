import { AppShell } from "@/components/app-shell";
import { Card, PageShell } from "@/components/primitives";

export default function Loading() {
  return (
    <AppShell>
      <PageShell>
        <div className="mb-6 space-y-2">
          <div className="h-7 w-40 rounded bg-elevated animate-pulse" />
          <div className="h-3.5 w-2/3 max-w-md rounded bg-elevated/60 animate-pulse" />
        </div>
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-[260px_minmax(0,1fr)]">
          <aside className="space-y-4 animate-pulse">
            <div className="h-9 w-full rounded-md bg-elevated" />
            <div className="space-y-2">
              <div className="h-3 w-20 rounded bg-elevated" />
              <div className="space-y-1">
                {[1, 2, 3, 4].map((i) => (
                  <div key={i} className="h-7 rounded bg-elevated/60" />
                ))}
              </div>
            </div>
            <div className="space-y-2">
              <div className="h-3 w-16 rounded bg-elevated" />
              <div className="space-y-1">
                {[1, 2, 3, 4, 5].map((i) => (
                  <div key={i} className="h-7 rounded bg-elevated/60" />
                ))}
              </div>
            </div>
          </aside>
          <section className="space-y-2">
            {[1, 2, 3, 4, 5].map((i) => (
              <Card key={i}>
                <div className="space-y-2 animate-pulse">
                  <div className="h-3 w-24 rounded bg-elevated" />
                  <div className="h-4 w-3/4 rounded bg-elevated" />
                  <div className="h-3 w-1/2 rounded bg-elevated/60" />
                </div>
              </Card>
            ))}
          </section>
        </div>
      </PageShell>
    </AppShell>
  );
}

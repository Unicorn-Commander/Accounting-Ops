// /open-items — Aaron's daily action surface.
//
// Audit issue C1: This route used to render a Sentinel findings dashboard.
// The richer seven-section OpenItemsView (Time-sensitive / Documents /
// Decisions / Waiting on Mosin / Waiting on attorney / Resolved / Recent AI
// Activity) is now the default. The Sentinel summary is demoted to a
// collapsible secondary block at the bottom that drills into /agent-activity
// for full detail.

import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { OpenItemsView } from "@/app/open-items/open-items-view";
import { SentinelDrilldown } from "@/app/open-items/sentinel-drilldown";
import { fetchOpenItems } from "@/lib/open-items";
import { fetchAIActivity } from "@/lib/ai-activity";
import { getActiveContext } from "@/lib/active-context";
import {
  listCheckRuns,
  listFindings,
  listScheduledActions,
  type SentinelCheckRun
} from "@/lib/sentinel";

export default async function OpenItemsPage({
  searchParams
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const sp = await searchParams;
  const ctx = await getActiveContext();
  const wid = ctx.activeWorkspace?.id;

  // Filter inputs from the URL — tax_year pill + entity scope flow through
  // here so refresh/back/forward navigation is sticky.
  const taxYearRaw = typeof sp.tax_year === "string" ? sp.tax_year : null;
  const taxYear = taxYearRaw ? parseInt(taxYearRaw, 10) : null;
  const entityIdRaw = typeof sp.entity_id === "string" ? sp.entity_id : null;
  // If user didn't pin an entity in the URL, use the cookie/default entity.
  const effectiveEntityId = entityIdRaw ?? ctx.activeEntity?.id ?? undefined;

  // Primary data: the seven-section open-items aggregator.
  const openItems = wid
    ? await fetchOpenItems(wid, {
        entityId: effectiveEntityId,
        taxYear: taxYear ?? undefined
      })
    : null;

  // Recent AI activity (Section 7).  Scope to entity if known.
  const aiActivity = wid
    ? await fetchAIActivity(wid, { entityId: effectiveEntityId, limit: 50 })
    : null;

  // Secondary data: Sentinel summary — kept available but demoted below the
  // owner-actionable list.  Computed lazily; if Sentinel is unhealthy we just
  // hide the block.
  const sentinelFindings = wid
    ? await listFindings(wid, { status: "open", days: 7, limit: 25 })
    : null;
  const sentinelRuns = wid
    ? await listCheckRuns(wid, { days: 7, limit: 100 })
    : null;
  const sentinelScheduled = wid
    ? await listScheduledActions(wid, { status: "pending", limit: 25 })
    : null;

  const sentinelTotals = {
    open: sentinelFindings?.items.length ?? 0,
    cycles: sentinelRuns?.cycles ?? 0,
    actions: (sentinelRuns?.items ?? []).reduce(
      (acc: number, r: SentinelCheckRun) => acc + (r.actions_taken ?? 0),
      0
    ),
    scheduled: sentinelScheduled?.items.length ?? 0
  };

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Open items"
          subtitle={
            ctx.activeWorkspace
              ? `${ctx.activeWorkspace.display_name} · what needs your attention right now`
              : "Sign in to view open items."
          }
        />

        {!wid ? (
          <Card>
            <p className="text-sm text-ink-tertiary">No active workspace.</p>
          </Card>
        ) : !openItems ? (
          <Card>
            <p className="text-sm text-ink-tertiary">
              Open-items service unavailable. Backend returned no data — try
              refreshing in a moment.
            </p>
            <p className="mt-2 text-[11px] text-ink-tertiary">
              If the issue persists, contact{" "}
              <a href="mailto:aaron@magicunicorn.tech" className="underline">
                aaron@magicunicorn.tech
              </a>
              .
            </p>
          </Card>
        ) : (
          <>
            <OpenItemsView
              data={openItems}
              workspaceId={wid}
              currentTaxYear={taxYear}
              aiActivity={aiActivity}
              entityId={effectiveEntityId}
            />

            {/* Secondary: Sentinel summary — promoted only as a footer
                drilldown.  Owner-actionable tasks are above; this is the
                "what the autopilot saw" rollup. */}
            <SentinelDrilldown
              totals={sentinelTotals}
              hasData={Boolean(sentinelFindings || sentinelRuns)}
            />
          </>
        )}
      </PageShell>
    </AppShell>
  );
}

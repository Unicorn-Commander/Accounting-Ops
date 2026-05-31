import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import {
  listCheckRuns,
  listFindings,
  listScheduledActions,
  type SentinelCheckRun,
  type SentinelFinding,
  type SentinelScheduledAction
} from "@/lib/sentinel";

function severityChip(s: SentinelFinding["severity"]) {
  switch (s) {
    case "critical":
    case "high":
      return <StatusChip tone="debit">{s}</StatusChip>;
    case "medium":
      return <StatusChip tone="pending">{s}</StatusChip>;
    case "low":
      return <StatusChip tone="ink">{s}</StatusChip>;
    default:
      return <StatusChip tone="ink">info</StatusChip>;
  }
}

function dateShort(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}

export default async function OpenItemsPage() {
  const ctx = await getActiveContext();
  const wid = ctx.activeWorkspace?.id;

  const findingsData = wid ? await listFindings(wid, { status: "open", days: 7, limit: 50 }) : null;
  const recentlyAck = wid
    ? await listFindings(wid, { status: "acknowledged", days: 30, limit: 25 })
    : null;
  const runsData = wid ? await listCheckRuns(wid, { days: 7, limit: 200 }) : null;
  const scheduledData = wid ? await listScheduledActions(wid, { status: "pending", limit: 50 }) : null;

  const findings = findingsData?.items ?? [];
  const cycles = runsData?.cycles ?? 0;
  const totalActions = (runsData?.items ?? []).reduce(
    (acc: number, r: SentinelCheckRun) => acc + (r.actions_taken ?? 0),
    0
  );
  const totalFindings = (runsData?.items ?? []).reduce(
    (acc: number, r: SentinelCheckRun) => acc + (r.findings_count ?? 0),
    0
  );

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Open items"
          subtitle={
            ctx.activeWorkspace
              ? `${ctx.activeWorkspace.display_name} · what Sentinel surfaced this week`
              : "Sign in to view Sentinel findings."
          }
        />

        {!wid ? (
          <Card>
            <p className="text-sm text-ink-tertiary">No active workspace.</p>
          </Card>
        ) : (
          <>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4 mb-4 md:mb-6">
              <Card padded={false}>
                <div className="p-4">
                  <p className="text-[10px] uppercase text-ink-tertiary tracking-wider">
                    Open findings
                  </p>
                  <p className="mt-2 font-mono text-2xl">{findings.length}</p>
                </div>
              </Card>
              <Card padded={false}>
                <div className="p-4">
                  <p className="text-[10px] uppercase text-ink-tertiary tracking-wider">
                    Cycles (7d)
                  </p>
                  <p className="mt-2 font-mono text-2xl">{cycles}</p>
                </div>
              </Card>
              <Card padded={false}>
                <div className="p-4">
                  <p className="text-[10px] uppercase text-ink-tertiary tracking-wider">
                    Actions (7d)
                  </p>
                  <p className="mt-2 font-mono text-2xl">{totalActions}</p>
                </div>
              </Card>
              <Card padded={false}>
                <div className="p-4">
                  <p className="text-[10px] uppercase text-ink-tertiary tracking-wider">
                    Scheduled
                  </p>
                  <p className="mt-2 font-mono text-2xl">{scheduledData?.items.length ?? 0}</p>
                </div>
              </Card>
            </div>

            {/* Sentinel activity (last 7 d) */}
            <Card className="mb-4 md:mb-6">
              <h2 className="text-sm font-semibold mb-3">Sentinel Activity (last 7d)</h2>
              {findings.length === 0 ? (
                <p className="text-sm text-ink-tertiary">
                  Sentinel hasn&apos;t surfaced anything new in the last 7 days.
                  This usually means the books are clean and no deadlines are imminent.
                </p>
              ) : (
                <div className="space-y-2">
                  {findings.map((f: SentinelFinding) => (
                    <div
                      key={f.id}
                      className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between rounded-md border border-default/60 bg-bg-soft p-3"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          {severityChip(f.severity)}
                          <span className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                            {f.check_type}
                          </span>
                          {f.entity_display_name ? (
                            <span className="text-[10px] text-ink-tertiary">
                              · {f.entity_display_name}
                            </span>
                          ) : null}
                        </div>
                        <p className="font-medium text-sm break-words">{f.title}</p>
                        <p className="mt-1 text-xs text-ink-secondary whitespace-pre-wrap line-clamp-3">
                          {f.summary}
                        </p>
                      </div>
                      <div className="text-[10px] text-ink-tertiary whitespace-nowrap shrink-0 sm:text-right">
                        {dateShort(f.created_at)}
                        {f.action_type ? (
                          <div className="mt-1">
                            <StatusChip tone="agent">{f.action_type}</StatusChip>
                          </div>
                        ) : null}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>

            {/* Upcoming scheduled actions */}
            <Card className="mb-4 md:mb-6">
              <h2 className="text-sm font-semibold mb-3">Upcoming scheduled actions</h2>
              {(scheduledData?.items ?? []).length === 0 ? (
                <p className="text-sm text-ink-tertiary">
                  Nothing scheduled. When Sentinel queues a future reminder (e.g.
                  &ldquo;send a Q3 estimate reminder on 2026-09-08&rdquo;), it will appear here.
                </p>
              ) : (
                <div className="space-y-2">
                  {(scheduledData?.items ?? []).map((a: SentinelScheduledAction) => (
                    <div
                      key={a.id}
                      className="flex items-center justify-between rounded-md border border-default/60 bg-bg-soft p-3"
                    >
                      <div className="min-w-0">
                        <p className="font-mono text-xs text-ink-tertiary">
                          {dateShort(a.scheduled_for)}
                        </p>
                        <p className="text-sm">{a.action_type}</p>
                      </div>
                      <StatusChip tone="pending">{a.status}</StatusChip>
                    </div>
                  ))}
                </div>
              )}
            </Card>

            {/* Recently acknowledged */}
            {recentlyAck && recentlyAck.items.length > 0 ? (
              <Card>
                <h2 className="text-sm font-semibold mb-3">Recently acknowledged (30d)</h2>
                <div className="space-y-1.5">
                  {recentlyAck.items.map((f: SentinelFinding) => (
                    <div
                      key={f.id}
                      className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between text-xs"
                    >
                      <span className="text-ink-secondary break-words">
                        {f.title}
                        {f.entity_display_name ? ` · ${f.entity_display_name}` : ""}
                      </span>
                      <span className="text-[10px] text-ink-tertiary whitespace-nowrap">
                        ack {dateShort(f.acknowledged_at)} by {f.acknowledged_by_uc_uid}
                      </span>
                    </div>
                  ))}
                </div>
              </Card>
            ) : null}

            <p className="mt-6 text-[10px] text-ink-tertiary">
              Total Sentinel findings (last 7d): {totalFindings}. Configure autonomy at{" "}
              <a href="/settings/sentinel" className="underline">/settings/sentinel</a>.
            </p>
          </>
        )}
      </PageShell>
    </AppShell>
  );
}

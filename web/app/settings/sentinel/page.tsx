import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { getSentinelConfig } from "@/lib/sentinel";
import { SentinelSettingsClient } from "./page-client";

export default async function SentinelSettingsPage() {
  const ctx = await getActiveContext();
  const wid = ctx.activeWorkspace?.id;
  const isAdmin = ctx.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;

  const config = wid ? await getSentinelConfig(wid) : null;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Sentinel Settings"
          subtitle={
            ctx.activeWorkspace
              ? `${ctx.activeWorkspace.display_name} · proactive CPA daemon`
              : "Sign in to configure Sentinel."
          }
        />

        {!wid ? (
          <Card>
            <p className="text-sm text-ink-tertiary">No active workspace.</p>
          </Card>
        ) : !config ? (
          <Card>
            <p className="text-sm text-ink-tertiary">
              Could not load Sentinel configuration. The backend may be unreachable
              or the migration may not have run yet.
            </p>
          </Card>
        ) : (
          <>
            <Card className="mb-4 md:mb-6">
              <h2 className="text-sm font-semibold mb-3">About Sentinel</h2>
              <p className="text-sm text-ink-secondary">
                Sentinel is the autonomous CPA daemon. It runs continuously,
                scans the workspace + the tax landscape, drafts memos + flags
                items for CPA review, and surfaces opportunities like a CPA
                partner would. Humans only intervene for judgment calls.
              </p>
              <p className="mt-2 text-xs text-ink-tertiary">
                Per-workspace: autonomy level + cadence + which checks run +
                notification preferences. Global kill switch:{" "}
                <code className="text-ink-secondary">SENTINEL_DISABLED=true</code>{" "}
                on the sentinel container env. Closed-period guard always
                applies; never auto-files; never auto-pays.
              </p>
              <p className="mt-2 text-xs text-ink-tertiary">
                Last cycle:{" "}
                {config.last_run_at ? (
                  new Date(config.last_run_at).toLocaleString()
                ) : (
                  <span className="text-ink-tertiary italic">never</span>
                )}
              </p>
            </Card>

            <Card>
              <h2 className="text-sm font-semibold mb-3">Configuration</h2>
              {!isAdmin ? (
                <>
                  <p className="text-sm text-ink-tertiary mb-4">
                    You have read-only access. An admin can change these settings.
                  </p>
                  <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
                    <div>
                      <dt className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                        Autonomy level
                      </dt>
                      <dd className="mt-1">
                        <StatusChip tone="agent">{config.autonomy_level}</StatusChip>
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                        Cadence
                      </dt>
                      <dd className="mt-1 font-mono">
                        every {config.check_interval_minutes} min
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                        Daily action cap
                      </dt>
                      <dd className="mt-1 font-mono">{config.max_actions_per_day}</dd>
                    </div>
                    <div>
                      <dt className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                        Notify
                      </dt>
                      <dd className="mt-1 text-xs">
                        {config.notify_owners ? "owners" : ""}
                        {config.notify_owners && config.notify_accountants ? ", " : ""}
                        {config.notify_accountants ? "accountants" : ""}
                        {!config.notify_owners && !config.notify_accountants ? "—" : ""}
                      </dd>
                    </div>
                    <div className="sm:col-span-2">
                      <dt className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                        Enabled checks
                      </dt>
                      <dd className="mt-1 text-xs flex flex-wrap gap-1.5">
                        {config.enabled_checks.map((c) => (
                          <StatusChip tone="ink" key={c}>
                            {c}
                          </StatusChip>
                        ))}
                      </dd>
                    </div>
                  </dl>
                </>
              ) : (
                <SentinelSettingsClient workspaceId={wid} initial={config} />
              )}
            </Card>
          </>
        )}
      </PageShell>
    </AppShell>
  );
}

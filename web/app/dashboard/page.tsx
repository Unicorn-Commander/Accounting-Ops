import Link from "next/link";
import { redirect } from "next/navigation";
import { ArrowRight, ArrowUpRight, Plus, Sparkles, Wifi } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { AgentActivitySection } from "@/components/agent-activity-section";
import { CfoBriefCard } from "@/components/cfo-brief-card";
import { EntitySelector } from "@/components/entity-selector";
import { Card, KpiCard, MaterialityBadge, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { getCashPosition, formatUSD } from "@/lib/cash-position";
import { fetchCfoBrief } from "@/lib/cfo-brief";
import { getProfitAndLoss } from "@/lib/journal";
import { fetchOpenItems } from "@/lib/open-items";
import { getRecentTransactions, shortDate, type LiveTransaction } from "@/lib/transactions";
import { cn } from "@/lib/cn";

export default async function DashboardPage() {
  const ctx = await getActiveContext();
  // If the active workspace has zero entities, the dashboard is meaningless —
  // send the user to /setup so the Setup Assistant can provision their first
  // entity. We only redirect when a workspace EXISTS but is empty; the
  // "no workspace at all" case is handled by EmptyWorkspaceState below.
  if (ctx.activeWorkspace && ctx.entities.length === 0) {
    redirect("/setup");
  }
  const canConsolidate = ctx.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;
  const entityFilter = ctx.activeEntity?.id && !ctx.isConsolidated ? ctx.activeEntity.id : undefined;
  const cashPosition = ctx.activeWorkspace
    ? await getCashPosition(ctx.activeWorkspace.id, entityFilter)
    : null;
  const txnData = ctx.activeWorkspace
    ? await getRecentTransactions(ctx.activeWorkspace.id, 30, 12, entityFilter)
    : null;
  const [plMtd, plYtd] = ctx.activeWorkspace
    ? await Promise.all([
        getProfitAndLoss(ctx.activeWorkspace.id, "mtd", entityFilter),
        getProfitAndLoss(ctx.activeWorkspace.id, "ytd", entityFilter)
      ])
    : [null, null];
  const openItemsCounts = ctx.activeWorkspace
    ? await fetchOpenItems(ctx.activeWorkspace.id, { entityId: entityFilter })
    : null;
  // CFO brief — the plain-language concierge header. Best-effort: returns null
  // on any backend failure, in which case we render the dashboard exactly as
  // before (the card simply doesn't appear).
  const cfoBrief = ctx.activeWorkspace
    ? await fetchCfoBrief(ctx.activeWorkspace.id, { entityId: entityFilter })
    : null;
  const liveTxns: LiveTransaction[] = txnData?.transactions ?? [];
  const subtitle = ctx.activeWorkspace
    ? `${ctx.activeWorkspace.display_name} · ${ctx.entities.length} ${ctx.entities.length === 1 ? "entity" : "entities"} · viewing ${ctx.isConsolidated ? "all entities (consolidated)" : ctx.activeEntity?.display_name ?? "—"}`
    : "Cash position, agent activity, and approvals across all your entities.";
  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Dashboard"
          subtitle={subtitle}
          action={
            ctx.activeWorkspace ? (
              <div className="flex flex-wrap items-center gap-2">
                <EntitySelector
                  entities={ctx.entities}
                  activeEntityId={ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id}
                  canConsolidate={canConsolidate}
                />
                <Link
                  href="/setup?mode=add"
                  className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-2.5 py-1 text-xs text-ink-secondary hover:border-brand-500/60 hover:text-ink-primary"
                  title="Add another entity to this workspace"
                >
                  <Plus size={12} />
                  Add entity
                </Link>
              </div>
            ) : null
          }
        />

        {!ctx.activeWorkspace ? (
          <EmptyWorkspaceState />
        ) : (
          <>
            {/* CFO brief — concierge header. Plain-language "what needs you now"
                + cash status + one-tap deep-links. Renders only when the
                deterministic brief endpoint succeeded; otherwise the dashboard
                degrades to exactly its prior content. The denser open-items
                banner + KPIs below act as the drill-downs. */}
            {cfoBrief ? <CfoBriefCard brief={cfoBrief} /> : null}

            {/* Open-items highlight banner — surfaces "what needs Aaron right now" */}
            {openItemsCounts && openItemsCounts.counts.action_required > 0 ? (
              <Link
                href="/open-items"
                className="group mb-5 block rounded-lg border border-debit/50 bg-gradient-to-r from-debit/[0.08] via-pending/[0.05] to-brand-500/[0.06] px-5 py-4 transition hover:border-debit/70 hover:shadow-brand-glow"
              >
                <div className="flex flex-wrap items-center gap-3">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-debit/15 text-debit">
                    <span className="font-mono text-base font-bold">
                      {openItemsCounts.counts.action_required}
                    </span>
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-semibold text-ink-primary">
                      You have {openItemsCounts.counts.action_required} open{" "}
                      {openItemsCounts.counts.action_required === 1
                        ? "item"
                        : "items"}{" "}
                      requiring your action
                    </p>
                    <p className="mt-0.5 text-xs text-ink-secondary">
                      {[
                        openItemsCounts.counts.time_sensitive > 0
                          ? `${openItemsCounts.counts.time_sensitive} time-sensitive`
                          : null,
                        openItemsCounts.counts.documents_pending > 0
                          ? `${openItemsCounts.counts.documents_pending} ${openItemsCounts.counts.documents_pending === 1 ? "doc" : "docs"} to provide`
                          : null,
                        openItemsCounts.counts.decisions_pending > 0
                          ? `${openItemsCounts.counts.decisions_pending} ${openItemsCounts.counts.decisions_pending === 1 ? "decision" : "decisions"}`
                          : null
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                  </div>
                  <span className="shrink-0 text-xs font-medium text-brand-300 group-hover:text-brand-400">
                    Open the list <ArrowRight size={12} className="inline" />
                  </span>
                </div>
              </Link>
            ) : openItemsCounts && openItemsCounts.counts.action_required === 0 ? (
              <Link
                href="/open-items"
                className="mb-5 block rounded-lg border border-credit/30 bg-credit/[0.04] px-5 py-3 transition hover:border-credit/50"
              >
                <div className="flex flex-wrap items-center gap-3">
                  <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-credit/15 text-credit">
                    ✓
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-credit">
                      No open items requiring action.
                    </p>
                    <p className="mt-0.5 text-[11px] text-ink-tertiary">
                      {openItemsCounts.counts.waiting_on_mosin} waiting on Mosin ·{" "}
                      {openItemsCounts.counts.resolved_this_week} resolved this week
                    </p>
                  </div>
                  <span className="text-xs text-ink-tertiary">
                    Open the list <ArrowRight size={11} className="inline" />
                  </span>
                </div>
              </Link>
            ) : null}

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <KpiCard
            label="Cash (assets)"
            value={cashPosition?.configured ? formatUSD(cashPosition.assets_total ?? cashPosition.total_usd) : "$—"}
            delta={
              cashPosition?.configured
                ? `Live · ${cashPosition.accounts.filter((a) => a.account_type !== "liability").length} cash + savings accounts`
                : "Connect SimpleFIN to see live"
            }
            deltaTone={cashPosition?.configured ? "credit" : "ink"}
          />
          <KpiCard
            label="Credit card balance"
            value={
              cashPosition?.configured
                ? `-${formatUSD(cashPosition.liabilities_total ?? "0")}`
                : "$—"
            }
            delta={
              cashPosition?.configured
                ? `${cashPosition.accounts.filter((a) => a.account_type === "liability").length} card${cashPosition.accounts.filter((a) => a.account_type === "liability").length === 1 ? "" : "s"} · what you owe`
                : "—"
            }
            deltaTone="debit"
          />
          <KpiCard
            label="Net cash position"
            value={cashPosition?.configured ? formatUSD(cashPosition.net_total ?? cashPosition.total_usd) : "$—"}
            delta="Cash − credit card balance"
            deltaTone={
              cashPosition && parseFloat(cashPosition.net_total ?? cashPosition.total_usd) >= 0
                ? "credit"
                : "debit"
            }
          />
          <KpiCard
            label="Net income (YTD)"
            value={plYtd ? formatUSD(plYtd.net_income) : "$—"}
            delta={
              plMtd
                ? `MTD ${formatUSD(plMtd.net_income)}`
                : "Live from journal hash chain"
            }
            deltaTone={plYtd && parseFloat(plYtd.net_income) >= 0 ? "credit" : "debit"}
          />
        </div>

        {/* Per-entity breakdown — only when viewing consolidated */}
        {cashPosition?.configured && ctx.isConsolidated && cashPosition.per_entity_assets && Object.keys(cashPosition.per_entity_assets).length > 0 && (
          <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4">
            {ctx.entities.map((e) => {
              const a = parseFloat(cashPosition.per_entity_assets?.[e.id] ?? "0");
              const l = parseFloat(cashPosition.per_entity_liabilities?.[e.id] ?? "0");
              if (a === 0 && l === 0) return null;
              const net = a - l;
              return (
                <Card key={e.id}>
                  <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                    {e.display_name}
                  </p>
                  <p className={cn("mt-1 font-mono text-base tabular-nums", net >= 0 ? "text-credit" : "text-debit")}>
                    {formatUSD(net)}
                  </p>
                  <div className="mt-2 space-y-0.5 text-[11px] text-ink-tertiary">
                    <div className="flex justify-between">
                      <span>Cash</span>
                      <span className="font-mono tabular-nums text-credit">{formatUSD(a)}</span>
                    </div>
                    {l > 0 && (
                      <div className="flex justify-between">
                        <span>Credit cards</span>
                        <span className="font-mono tabular-nums text-debit">-{formatUSD(l)}</span>
                      </div>
                    )}
                  </div>
                </Card>
              );
            })}
          </div>
        )}

        {/* Revenue/Expense secondary KPIs — kept for at-a-glance P&L health */}
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
          <KpiCard
            label="Revenue (YTD)"
            value={plYtd ? formatUSD(plYtd.revenue.total) : "$—"}
            delta={plYtd ? `${plYtd.revenue.lines.length} categories` : "Post entries to see"}
            deltaTone="credit"
          />
          <KpiCard
            label="Expenses (YTD)"
            value={plYtd ? formatUSD(plYtd.expenses.total) : "$—"}
            delta={plYtd ? `${plYtd.expenses.lines.length} categories` : "Post entries to see"}
            deltaTone="debit"
          />
        </div>

        <div className="mt-8 grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <Card padded={false}>
              <div className="flex items-center justify-between border-b border-subtle px-6 py-4">
                <div>
                  <h2 className="text-base font-semibold">Recent transactions</h2>
                  <p className="mt-0.5 text-xs text-ink-tertiary">Last 30 days across connected accounts</p>
                </div>
                <Link
                  href="/transactions"
                  className="inline-flex items-center gap-1 text-xs text-ink-secondary hover:text-ink-primary"
                >
                  View all <ArrowRight size={13} />
                </Link>
              </div>
              {liveTxns.length > 0 ? (
                <div className="overflow-x-auto"><table className="w-full min-w-[600px] text-sm">
                  <thead className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                    <tr className="border-b border-subtle">
                      <th className="px-6 py-2.5 text-left font-medium">Date</th>
                      <th className="px-2 py-2.5 text-left font-medium">Payee · Account</th>
                      <th className="px-2 py-2.5 text-left font-medium">Status</th>
                      <th className="px-2 py-2.5 text-left font-medium">Category</th>
                      <th className="px-2 py-2.5 text-center font-medium">M</th>
                      <th className="px-6 py-2.5 text-right font-medium">Amount</th>
                    </tr>
                  </thead>
                  <tbody>
                    {liveTxns.map((t) => {
                      const amt = parseFloat(t.amount);
                      const isCredit = amt >= 0;
                      const absAmt = Math.abs(amt);
                      const materiality: "L" | "M" | "H" =
                        absAmt < 100 ? "L" : absAmt < 1000 ? "M" : "H";
                      const catTone =
                        t.category_status === "auto"
                          ? "credit"
                          : t.category_status === "suggest"
                            ? "pending"
                            : "ink";
                      return (
                        <tr
                          key={t.id}
                          className="group cursor-pointer border-b border-subtle/60 last:border-b-0 hover:bg-white/[0.02]"
                        >
                          <td className="whitespace-nowrap px-6 py-3 font-mono text-xs text-ink-tertiary">
                            {shortDate(t.posted_at)}
                          </td>
                          <td className="px-2 py-3">
                            <div className="font-medium truncate max-w-[260px]" title={t.payee ?? t.description}>
                              {t.payee ?? t.description ?? "—"}
                            </div>
                            <div className="text-xs text-ink-tertiary truncate" title={`${t.org_name} · ${t.account_name}`}>
                              {t.org_name} · {t.account_name}
                            </div>
                          </td>
                          <td className="px-2 py-3">
                            {t.is_pending ? (
                              <StatusChip tone="pending">Pending</StatusChip>
                            ) : (
                              <StatusChip tone="ink">Posted</StatusChip>
                            )}
                          </td>
                          <td className="px-2 py-3">
                            {t.category ? (
                              <StatusChip tone={catTone as "credit" | "pending" | "ink"}>
                                {t.category.length > 18 ? `${t.category.slice(0, 17)}…` : t.category}
                              </StatusChip>
                            ) : (
                              <span className="text-[11px] text-ink-disabled">uncategorized</span>
                            )}
                          </td>
                          <td className="px-2 py-3 text-center">
                            <MaterialityBadge level={materiality} />
                          </td>
                          <td
                            className={cn(
                              "px-6 py-3 text-right font-mono tabular-nums",
                              isCredit ? "text-credit" : "text-ink-primary"
                            )}
                          >
                            {isCredit ? "+" : "-"}
                            {formatUSD(absAmt)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table></div>
              ) : (
                <div className="px-6 py-10 text-center">
                  <p className="text-sm text-ink-secondary mb-1">
                    {txnData?.configured
                      ? "No transactions in the last 30 days."
                      : "No live transaction feed yet."}
                  </p>
                  <p className="text-xs text-ink-tertiary mb-4">
                    {txnData?.configured
                      ? "Activity across your connected accounts will show up here as it posts."
                      : "Connect a bank aggregator to start populating your transaction history."}
                  </p>
                  <Link
                    href="/connectors"
                    className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600"
                  >
                    {txnData?.configured ? "Manage connectors" : "Connect a bank"}
                    <ArrowRight size={11} />
                  </Link>
                </div>
              )}
            </Card>
          </div>

          <div className="space-y-4">
            <Card padded={false}>
              <div className="flex items-center justify-between border-b border-subtle px-5 py-4">
                <div>
                  <h2 className="text-base font-semibold">Cash by account</h2>
                  <p className="mt-0.5 text-xs text-ink-tertiary">
                    {cashPosition?.configured ? (
                      <span className="inline-flex items-center gap-1.5">
                        <Wifi size={11} className="text-credit" />
                        Live · SimpleFIN ·{" "}
                        {cashPosition.accounts[0]?.balance_as_of
                          ? new Date(cashPosition.accounts[0].balance_as_of * 1000).toLocaleString(
                              "en-US",
                              { dateStyle: "short", timeStyle: "short" }
                            )
                          : ""}
                      </span>
                    ) : (
                      "Connect a banking aggregator to see live balances"
                    )}
                  </p>
                </div>
                <Link
                  href="/connectors"
                  className="text-[11px] text-ink-secondary hover:text-ink-primary"
                >
                  Connectors →
                </Link>
              </div>
              <ul className="divide-y divide-subtle/60">
                {cashPosition?.configured && cashPosition.accounts.length > 0 ? (
                  cashPosition.accounts.map((acct) => {
                    const balanceNum = parseFloat(acct.balance);
                    const isCredit = balanceNum >= 0;
                    return (
                      <li key={acct.id} className="flex items-center justify-between px-5 py-2.5">
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-sm text-ink-secondary">{acct.name}</div>
                          <div className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                            {acct.org_name}
                          </div>
                        </div>
                        <span
                          className={cn(
                            "shrink-0 font-mono text-sm tabular-nums",
                            isCredit ? "text-credit" : "text-debit"
                          )}
                        >
                          {formatUSD(acct.balance)}
                        </span>
                      </li>
                    );
                  })
                ) : (
                  <li className="px-5 py-8 text-center">
                    <p className="text-xs text-ink-secondary mb-3">
                      No bank accounts connected yet.
                    </p>
                    <Link
                      href="/connectors"
                      className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary"
                    >
                      Connect SimpleFIN
                      <ArrowRight size={11} />
                    </Link>
                  </li>
                )}
              </ul>
            </Card>

            <Card padded={false}>
              <div className="flex items-center gap-2 border-b border-subtle px-5 py-3">
                <Sparkles size={12} className="text-agent-500" />
                <h2 className="text-sm font-semibold">Compliance watch</h2>
                <Link
                  href="/specialty-rules"
                  className="ml-auto text-[11px] text-ink-secondary hover:text-ink-primary"
                >
                  Review rules <ArrowUpRight size={11} className="inline" />
                </Link>
              </div>
              <div className="px-5 py-6 text-center text-xs text-ink-tertiary">
                <p>No active rules yet.</p>
                <p className="mt-1">
                  Add a 1099-NEC trigger, sales-tax nexus tracker, FERS earned-income watch,
                  or any other rule on{" "}
                  <Link
                    href="/specialty-rules"
                    className="text-brand-400 underline-offset-2 hover:underline"
                  >
                    Specialty rules
                  </Link>{" "}
                  to populate this panel.
                </p>
              </div>
            </Card>
          </div>
        </div>

        <div className="mt-8">
          <AgentActivitySection />
        </div>
          </>
        )}
      </PageShell>
    </AppShell>
  );
}

// ---------------------------------------------------------------------------
// Empty / no-workspace state
// ---------------------------------------------------------------------------

function EmptyWorkspaceState() {
  return (
    <Card>
      <div className="flex flex-col items-center gap-3 py-10 text-center">
        <Sparkles size={32} className="text-ink-disabled" />
        <p className="text-base font-medium text-ink-secondary">
          No workspaces yet.
        </p>
        <p className="max-w-md text-xs text-ink-tertiary">
          Workspaces are how Accounting-Ops scopes your entities, journal,
          and approvals. Sign in with the workspace owner&apos;s account, or
          have them provision a membership for you.
        </p>
        <Link
          href="/login"
          className="mt-1 inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600"
        >
          Sign in to your workspace
        </Link>
      </div>
    </Card>
  );
}

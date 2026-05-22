import Link from "next/link";
import { Hash, Shield } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import {
  Card,
  MaterialityBadge,
  PageShell,
  SectionHeader,
  StatusChip
} from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import {
  formatAmount,
  getJournalEntries,
  shortHash,
  type JournalEntry
} from "@/lib/journal";
import { shortDate } from "@/lib/transactions";
import { VerifyChainButton } from "@/components/verify-chain-button";
import { cn } from "@/lib/cn";

type SearchParams = {
  period?: string;
  start?: string;
  end?: string;
  category?: string;
  status?: string;
  q?: string;
};

export default async function JournalPage(props: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await props.searchParams;
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const entityFilter =
    ctx.activeEntity?.id && !ctx.isConsolidated ? ctx.activeEntity.id : undefined;
  const canConsolidate = ctx.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;

  const data = workspace
    ? await getJournalEntries(workspace.id, {
        entityId: entityFilter,
        startDate: sp.start,
        endDate: sp.end,
        category: sp.category,
        statusFilter: sp.status,
        search: sp.q,
        limit: 200
      })
    : null;

  const entries: JournalEntry[] = data?.entries ?? [];
  const total = data?.total ?? 0;
  const categories = data?.categories ?? [];

  // Aggregate by entity for the summary strip
  const entityTotals = new Map<
    string,
    { name: string; entityId: string | null; count: number; sum: number }
  >();
  for (const e of entries) {
    const k = e.entity_display_name ?? "—";
    const cur = entityTotals.get(k) ?? {
      name: k,
      entityId: e.entity_id,
      count: 0,
      sum: 0
    };
    cur.count += 1;
    cur.sum += parseFloat(e.amount);
    entityTotals.set(k, cur);
  }

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Journal"
          subtitle={
            workspace
              ? `${total} ${total === 1 ? "entry" : "entries"} · hash-chained, append-only`
              : "Sign in to view journal."
          }
          action={
            <EntitySelector
              entities={ctx.entities}
              activeEntityId={ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id}
              canConsolidate={canConsolidate}
              size="sm"
            />
          }
        />

        {/* Summary strip */}
        {entityTotals.size > 0 && workspace && (
          <div className="mb-4 grid grid-cols-1 gap-3 md:grid-cols-4">
            {Array.from(entityTotals.values()).map((t) => (
              <Card key={t.name}>
                <div className="flex items-start justify-between">
                  <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                    {t.name}
                  </p>
                  {t.entityId && (
                    <VerifyChainButton workspaceId={workspace.id} entityId={t.entityId} />
                  )}
                </div>
                <p className="mt-1 font-mono text-sm text-ink-primary tabular-nums">
                  {formatAmount(t.sum)}
                </p>
                <p className="mt-0.5 text-[11px] text-ink-tertiary">
                  {t.count} {t.count === 1 ? "entry" : "entries"}
                </p>
              </Card>
            ))}
          </div>
        )}

        <div className="mb-4 flex justify-end">
          <Link
            href="/journal/new"
            className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600"
          >
            + Manual entry
          </Link>
        </div>

        <Card padded={false}>
          <form
            method="GET"
            action="/journal"
            className="flex flex-wrap items-center gap-2 border-b border-subtle px-5 py-3"
          >
            <input
              type="search"
              name="q"
              defaultValue={sp.q ?? ""}
              placeholder="Search payee, description, account…"
              className="min-w-[240px] flex-1 rounded-md border border-default bg-canvas px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none"
            />
            <input
              type="date"
              name="start"
              defaultValue={sp.start ?? ""}
              className="rounded-md border border-default bg-surface px-2 py-1.5 text-xs"
            />
            <input
              type="date"
              name="end"
              defaultValue={sp.end ?? ""}
              className="rounded-md border border-default bg-surface px-2 py-1.5 text-xs"
            />
            <select
              name="category"
              defaultValue={sp.category ?? ""}
              className="rounded-md border border-default bg-surface px-3 py-1.5 text-xs"
            >
              <option value="">All categories</option>
              {categories.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
            <select
              name="status"
              defaultValue={sp.status ?? ""}
              className="rounded-md border border-default bg-surface px-3 py-1.5 text-xs"
            >
              <option value="">All statuses</option>
              <option value="posted">Posted</option>
              <option value="reversed">Reversed</option>
            </select>
            <button
              type="submit"
              className="rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600"
            >
              Apply
            </button>
            {(sp.q || sp.start || sp.end || sp.category || sp.status) && (
              <Link
                href="/journal"
                className="rounded-md border border-default bg-surface px-3 py-1.5 text-xs text-ink-tertiary hover:bg-elevated"
              >
                Clear
              </Link>
            )}
          </form>

          {entries.length === 0 ? (
            <div className="px-6 py-12 text-center text-sm text-ink-tertiary">
              No journal entries match these filters.{" "}
              {total === 0 && (
                <>
                  <Link
                    href="/reconciliation-queue"
                    className="text-brand-400 hover:underline"
                  >
                    Post your first entry from the reconciliation queue →
                  </Link>
                </>
              )}
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                <tr className="border-b border-subtle">
                  <th className="px-5 py-2.5 text-left font-medium">Date</th>
                  <th className="px-2 py-2.5 text-left font-medium">Entity</th>
                  <th className="px-2 py-2.5 text-left font-medium">DR / CR</th>
                  <th className="px-2 py-2.5 text-left font-medium">Description</th>
                  <th className="px-2 py-2.5 text-center font-medium">M</th>
                  <th className="px-2 py-2.5 text-left font-medium" title="Hash chain (truncated)">
                    Hash
                  </th>
                  <th className="px-5 py-2.5 text-right font-medium">Amount</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => (
                  <tr
                    key={e.id}
                    className={cn(
                      "cursor-pointer border-b border-subtle/60 last:border-b-0 hover:bg-white/[0.02]",
                      e.status === "reversed" && "opacity-50 line-through"
                    )}
                  >
                    <td className="whitespace-nowrap px-5 py-3 font-mono text-xs text-ink-tertiary">
                      <Link href={`/journal/${e.id}`} className="hover:text-ink-primary">
                        {shortDate(e.effective_date)}
                      </Link>
                    </td>
                    <td className="px-2 py-3">
                      {e.entity_display_name && (
                        <span className="inline-flex items-center rounded-full bg-brand-500/10 px-2 py-0.5 text-[10px] font-medium text-brand-300">
                          {e.entity_display_name}
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-3 text-[11px] font-mono">
                      <div className="text-debit">{e.debit_account}</div>
                      <div className="text-credit">{e.credit_account}</div>
                    </td>
                    <td className="px-2 py-3">
                      <div className="max-w-[260px] truncate font-medium" title={e.description ?? ""}>
                        {e.payee ?? e.description ?? "—"}
                      </div>
                      <div className="max-w-[260px] truncate text-[10px] text-ink-tertiary">
                        {e.category} · {e.source ?? "manual"}
                      </div>
                    </td>
                    <td className="px-2 py-3 text-center">
                      {e.materiality_tier && (
                        <MaterialityBadge level={e.materiality_tier} />
                      )}
                    </td>
                    <td className="px-2 py-3 font-mono text-[10px] text-ink-tertiary" title={e.this_hash_hex}>
                      <Hash size={10} className="mr-1 inline" />
                      {shortHash(e.this_hash_hex)}
                    </td>
                    <td className="px-5 py-3 text-right font-mono tabular-nums text-ink-primary">
                      {formatAmount(e.amount)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <p className="mt-4 flex items-center gap-2 text-[11px] text-ink-tertiary">
          <Shield size={11} />
          Every entry is part of a per-entity SHA-256 hash chain. Mutations create
          reversing entries — the chain is never rewritten.
        </p>
      </PageShell>
    </AppShell>
  );
}

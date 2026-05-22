"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { Filter, Search, X } from "lucide-react";
import {
  Card,
  ConfidenceDot,
  MaterialityBadge,
  StatusChip
} from "@/components/primitives";
import { formatUSD } from "@/lib/cash-position";
import { shortDate, type LiveTransaction } from "@/lib/transactions";
import { cn } from "@/lib/cn";

type TxnAccount = {
  id: string;
  name: string;
  org_name: string;
};

export function LiveTransactionsTable({
  transactions,
  daysWindow,
  totalCount,
  configured,
  emptyAction
}: {
  transactions: LiveTransaction[];
  daysWindow: number;
  totalCount: number;
  configured: boolean;
  emptyAction?: React.ReactNode;
}) {
  const accounts: TxnAccount[] = useMemo(() => {
    const seen = new Map<string, TxnAccount>();
    for (const t of transactions) {
      if (!seen.has(t.account_id)) {
        seen.set(t.account_id, {
          id: t.account_id,
          name: t.account_name,
          org_name: t.org_name
        });
      }
    }
    return Array.from(seen.values()).sort((a, b) =>
      a.org_name.localeCompare(b.org_name) || a.name.localeCompare(b.name)
    );
  }, [transactions]);

  const orgs = useMemo(() => {
    const set = new Set<string>();
    for (const t of transactions) set.add(t.org_name);
    return Array.from(set).sort();
  }, [transactions]);

  const showEntityColumn = useMemo(
    () => transactions.some((t) => t.entity_id || t.entity_display_name),
    [transactions]
  );

  const showCategoryColumn = useMemo(
    () => transactions.some((t) => t.category),
    [transactions]
  );

  const [accountId, setAccountId] = useState<string>("all");
  const [orgName, setOrgName] = useState<string>("all");
  const [categoryFilter, setCategoryFilter] = useState<string>("all");
  const [includePending, setIncludePending] = useState(true);
  const [search, setSearch] = useState("");

  const categories = useMemo(() => {
    const set = new Set<string>();
    for (const t of transactions) if (t.category) set.add(t.category);
    return Array.from(set).sort();
  }, [transactions]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return transactions.filter((t) => {
      if (accountId !== "all" && t.account_id !== accountId) return false;
      if (orgName !== "all" && t.org_name !== orgName) return false;
      if (categoryFilter === "needs_review" && t.category_status !== "unknown" && t.category_sign_matched !== false) return false;
      if (categoryFilter !== "all" && categoryFilter !== "needs_review" && t.category !== categoryFilter) return false;
      if (!includePending && t.is_pending) return false;
      if (q) {
        const blob = `${t.payee ?? ""} ${t.description ?? ""} ${t.memo ?? ""} ${t.org_name} ${t.account_name} ${t.amount} ${t.entity_display_name ?? ""} ${t.category ?? ""}`.toLowerCase();
        if (!blob.includes(q)) return false;
      }
      return true;
    });
  }, [transactions, accountId, orgName, categoryFilter, includePending, search]);

  const inflow = useMemo(
    () => filtered.reduce((s, t) => s + Math.max(parseFloat(t.amount), 0), 0),
    [filtered]
  );
  const outflow = useMemo(
    () => filtered.reduce((s, t) => s + Math.min(parseFloat(t.amount), 0), 0),
    [filtered]
  );
  const pendingCount = useMemo(
    () => filtered.filter((t) => t.is_pending).length,
    [filtered]
  );

  function clearFilters() {
    setAccountId("all");
    setOrgName("all");
    setIncludePending(true);
    setSearch("");
  }

  const filtersActive =
    accountId !== "all" || orgName !== "all" || !includePending || search.length > 0;

  if (!configured) {
    return (
      <Card padded={false}>
        <div className="px-6 py-12 text-center text-sm text-ink-tertiary">
          No bank accounts connected yet.{" "}
          <Link
            href="/connectors"
            className="text-ink-secondary underline-offset-2 hover:text-ink-primary hover:underline"
          >
            Connect a bank
          </Link>{" "}
          to populate this view.
        </div>
      </Card>
    );
  }

  return (
    <Card padded={false}>
      <div className="flex flex-wrap items-center gap-2 border-b border-subtle px-5 py-3">
        <div className="relative min-w-[240px] flex-1">
          <Search
            size={14}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-tertiary"
          />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search payee, memo, amount..."
            className="w-full rounded-md border border-default bg-canvas py-1.5 pl-9 pr-3 text-sm text-ink-primary placeholder:text-ink-disabled focus:border-brand-500 focus:outline-none"
          />
        </div>

        <select
          value={orgName}
          onChange={(e) => {
            setOrgName(e.target.value);
            setAccountId("all");
          }}
          className="rounded-md border border-default bg-surface px-3 py-1.5 text-xs"
        >
          <option value="all">All banks ({orgs.length})</option>
          {orgs.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>

        <select
          value={accountId}
          onChange={(e) => setAccountId(e.target.value)}
          className="min-w-[200px] rounded-md border border-default bg-surface px-3 py-1.5 text-xs"
        >
          <option value="all">All accounts</option>
          {accounts
            .filter((a) => orgName === "all" || a.org_name === orgName)
            .map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} ({a.org_name})
              </option>
            ))}
        </select>

        {showCategoryColumn && (
          <select
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value)}
            className="rounded-md border border-default bg-surface px-3 py-1.5 text-xs"
          >
            <option value="all">All categories</option>
            <option value="needs_review">Needs review</option>
            {categories.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        )}

        <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-default bg-surface px-3 py-1.5 text-xs text-ink-secondary hover:bg-elevated">
          <input
            type="checkbox"
            className="h-3 w-3 accent-brand-500"
            checked={includePending}
            onChange={(e) => setIncludePending(e.target.checked)}
          />
          Include pending
        </label>

        {filtersActive && (
          <button
            onClick={clearFilters}
            className="inline-flex items-center gap-1 rounded-md border border-default bg-surface px-2 py-1.5 text-[10px] text-ink-tertiary hover:bg-elevated hover:text-ink-primary"
          >
            <X size={11} /> Clear
          </button>
        )}

        <span className="ml-auto text-[11px] text-ink-tertiary">
          {filtered.length}
          {filtered.length !== transactions.length ? ` of ${transactions.length}` : ""}{" "}
          {filtered.length === 1 ? "transaction" : "transactions"} · last {daysWindow}d
          {pendingCount > 0 ? ` · ${pendingCount} pending` : ""}
        </span>
      </div>

      <div className="grid grid-cols-3 gap-px border-b border-subtle bg-elevated">
        <SummaryStat label="Inflow" value={`+${formatUSD(inflow)}`} tone="credit" />
        <SummaryStat label="Outflow" value={formatUSD(outflow)} tone="debit" />
        <SummaryStat label="Net" value={formatUSD(inflow + outflow)} tone={inflow + outflow >= 0 ? "credit" : "debit"} />
      </div>

      {filtered.length === 0 ? (
        <div className="px-6 py-12 text-center text-sm text-ink-tertiary">
          No transactions match these filters.
          {emptyAction}
        </div>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-[10px] uppercase tracking-wider text-ink-tertiary">
            <tr className="border-b border-subtle">
              <th className="px-5 py-2.5 text-left font-medium">Date</th>
              <th className="px-2 py-2.5 text-left font-medium">Payee</th>
              <th className="px-2 py-2.5 text-left font-medium">Account</th>
              {showEntityColumn && (
                <th className="px-2 py-2.5 text-left font-medium">Entity</th>
              )}
              {showCategoryColumn && (
                <th className="px-2 py-2.5 text-left font-medium">Category</th>
              )}
              <th className="px-2 py-2.5 text-left font-medium">Status</th>
              <th className="px-2 py-2.5 text-center font-medium">M</th>
              <th className="px-2 py-2.5 text-center font-medium">Conf</th>
              <th className="px-5 py-2.5 text-right font-medium">Amount</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((t) => {
              const amt = parseFloat(t.amount);
              const isCredit = amt >= 0;
              const absAmt = Math.abs(amt);
              const materiality: "L" | "M" | "H" =
                absAmt < 100 ? "L" : absAmt < 1000 ? "M" : "H";
              return (
                <tr
                  key={t.id}
                  className="group cursor-pointer border-b border-subtle/60 last:border-b-0 hover:bg-white/[0.02]"
                >
                  <td className="whitespace-nowrap px-5 py-3 font-mono text-xs text-ink-tertiary">
                    {shortDate(t.posted_at)}
                  </td>
                  <td className="px-2 py-3">
                    <div
                      className="max-w-[300px] truncate font-medium"
                      title={t.payee ?? t.description}
                    >
                      {t.payee ?? t.description ?? "—"}
                    </div>
                    <div
                      className="max-w-[300px] truncate text-[10px] text-ink-tertiary"
                      title={t.description}
                    >
                      {t.description}
                    </div>
                  </td>
                  <td className="px-2 py-3">
                    <div className="text-xs font-medium">{t.org_name}</div>
                    <div className="text-[10px] text-ink-tertiary">{t.account_name}</div>
                  </td>
                  {showEntityColumn && (
                    <td className="px-2 py-3">
                      {t.entity_display_name ? (
                        <span className="inline-flex items-center rounded-full bg-brand-500/10 px-2 py-0.5 text-[10px] font-medium text-brand-300">
                          {t.entity_display_name}
                        </span>
                      ) : (
                        <span className="inline-flex items-center rounded-full bg-pending/10 px-2 py-0.5 text-[10px] font-medium text-pending">
                          Unmapped
                        </span>
                      )}
                    </td>
                  )}
                  {showCategoryColumn && (
                    <td className="px-2 py-3">
                      <CategoryPill
                        category={t.category}
                        status={t.category_status}
                        signMatched={t.category_sign_matched}
                      />
                    </td>
                  )}
                  <td className="px-2 py-3">
                    {t.is_pending ? (
                      <StatusChip tone="pending">Pending</StatusChip>
                    ) : (
                      <StatusChip tone="ink">Posted</StatusChip>
                    )}
                  </td>
                  <td className="px-2 py-3 text-center">
                    <MaterialityBadge level={materiality} />
                  </td>
                  <td className="px-2 py-3 text-center">
                    <ConfidenceDot value={0} />
                  </td>
                  <td
                    className={cn(
                      "px-5 py-3 text-right font-mono tabular-nums",
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
        </table>
      )}
    </Card>
  );
}

function CategoryPill({
  category,
  status,
  signMatched
}: {
  category?: string | null;
  status?: "auto" | "suggest" | "unknown" | null;
  signMatched?: boolean | null;
}) {
  if (!category || category === "Uncategorized" || status === "unknown") {
    return (
      <span className="inline-flex items-center rounded-full bg-ink-tertiary/10 px-2 py-0.5 text-[10px] font-medium text-ink-tertiary">
        Uncategorized
      </span>
    );
  }
  if (status === "suggest" || signMatched === false) {
    return (
      <span
        className="inline-flex items-center rounded-full bg-pending/15 px-2 py-0.5 text-[10px] font-medium text-pending"
        title="Suggested — needs human review"
      >
        {category}
      </span>
    );
  }
  return (
    <span
      className="inline-flex items-center rounded-full bg-credit/10 px-2 py-0.5 text-[10px] font-medium text-credit"
      title={`Auto-categorized via ${category}`}
    >
      {category}
    </span>
  );
}

function SummaryStat({
  label,
  value,
  tone
}: {
  label: string;
  value: string;
  tone: "credit" | "debit" | "ink";
}) {
  const cls =
    tone === "credit" ? "text-credit" : tone === "debit" ? "text-debit" : "text-ink-primary";
  return (
    <div className="bg-surface px-5 py-3">
      <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
        {label}
      </p>
      <p className={cn("mt-1 font-mono text-base tabular-nums", cls)}>{value}</p>
    </div>
  );
}

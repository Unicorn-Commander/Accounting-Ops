"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2, Pencil, Plus, Trash2, X, Check } from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";

type Entity = { id: string; display_name: string };

type CoaAccount = {
  id: string;
  account_number: string;
  account_name: string;
  canonical_form: string | null;
  account_type: string;
  normal_balance: string;
  linked_external_source?: string | null;
  linked_external_account_id?: string | null;
  is_active: boolean;
  is_system: boolean;
  notes: string | null;
};

const ACCOUNT_TYPE_ORDER = [
  "asset",
  "liability",
  "equity",
  "revenue",
  "cogs",
  "expense",
  "other_income",
  "other_expense",
  "tax",
] as const;

const ACCOUNT_TYPE_LABELS: Record<string, string> = {
  asset: "Assets",
  liability: "Liabilities",
  equity: "Equity",
  revenue: "Revenue",
  cogs: "Cost of Goods Sold",
  expense: "Expenses",
  other_income: "Other Income",
  other_expense: "Other Expenses",
  tax: "Tax",
};

type NewAccount = {
  account_number: string;
  account_name: string;
  account_type: string;
};

export function CoaEditor({
  workspaceId,
  entities,
  activeEntityId,
  initialAccounts = [],
}: {
  workspaceId: string;
  entities: Entity[];
  activeEntityId: string;
  initialAccounts?: CoaAccount[];
}) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [entityId, setEntityId] = useState(activeEntityId);
  // Hydrated from server-rendered prefetch; entity-switch triggers a
  // router.refresh() rather than a client-side fetch because the proxy at
  // /api/coa/[wid]/[eid] is shadowed by Traefik in production.
  const [accounts, setAccounts] = useState<CoaAccount[]>(initialAccounts);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editType, setEditType] = useState("");
  const [showNewForm, setShowNewForm] = useState(false);
  const [newAccount, setNewAccount] = useState<NewAccount>({
    account_number: "",
    account_name: "",
    account_type: "expense",
  });
  const [deactivating, setDeactivating] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  // Keep local accounts in sync whenever the server re-renders with a new
  // initialAccounts (e.g. after entity switch via ?entity= query param).
  useEffect(() => {
    setAccounts(initialAccounts);
  }, [initialAccounts]);

  // Switching entity → bump ?entity= query so server re-renders with the
  // new entity's COA. The CoaPage server component re-runs and prefetches.
  function switchEntity(newEntityId: string) {
    setEntityId(newEntityId);
    const params = new URLSearchParams(searchParams?.toString() ?? "");
    params.set("entity", newEntityId);
    router.push(`?${params.toString()}`);
    router.refresh();
  }

  async function handleCreate() {
    if (!newAccount.account_number || !newAccount.account_name) return;
    setCreating(true);
    setError(null);
    try {
      const res = await fetch(`/api/coa/${workspaceId}/${entityId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_number: newAccount.account_number,
          account_name: newAccount.account_name,
          account_type: newAccount.account_type,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(body.detail ?? "Create failed");
      }
      const created = await res.json();
      setAccounts((prev) =>
        [...prev, created].sort((a, b) => a.account_number.localeCompare(b.account_number))
      );
      setNewAccount({ account_number: "", account_name: "", account_type: "expense" });
      setShowNewForm(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Create failed");
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(account: CoaAccount) {
    if (account.is_system) {
      setError("System accounts cannot be deactivated.");
      return;
    }
    setDeactivating(account.id);
    setError(null);
    try {
      const res = await fetch(`/api/coa/${workspaceId}/${entityId}?accountId=${account.id}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(body.detail ?? "Deactivate failed");
      }
      setAccounts((prev) =>
        prev.map((a) =>
          a.id === account.id ? { ...a, is_active: false } : a
        )
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Deactivate failed");
    } finally {
      setDeactivating(null);
    }
  }

  async function handleSaveEdit(account: CoaAccount) {
    if (!editName.trim()) return;
    setError(null);
    const payload: Record<string, string> = { account_name: editName.trim() };
    if (editType !== account.account_type) {
      payload.account_type = editType;
    }
    try {
      const res = await fetch(`/api/coa/${workspaceId}/${entityId}?accountId=${account.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(body.detail ?? "Update failed");
      }
      const updated = await res.json();
      setAccounts((prev) =>
        prev.map((a) =>
          a.id === account.id
            ? {
                ...a,
                account_name: updated.account_name,
                account_type: updated.account_type,
                canonical_form: `${a.account_number}·${updated.account_name}`,
              }
            : a
        )
      );
      setEditingId(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Update failed");
    }
  }

  const grouped: Record<string, CoaAccount[]> = {};
  for (const a of accounts) {
    (grouped[a.account_type] ??= []).push(a);
  }

  return (
    <div className="space-y-6">
      {error && (
        <div className="rounded-md border border-debit/40 bg-debit/[0.06] px-3 py-2 text-sm text-debit">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-12 text-sm text-ink-tertiary">
          <Loader2 size={14} className="mr-2 animate-spin" />
          Loading chart of accounts…
        </div>
      ) : (
        <>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <select
                value={entityId}
                onChange={(e) => switchEntity(e.target.value)}
                className="rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
              >
                {entities.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.display_name}
                  </option>
                ))}
              </select>
            </div>
            <button
              onClick={() => setShowNewForm(!showNewForm)}
              className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white shadow-brand-glow hover:bg-brand-600 transition"
            >
              {showNewForm ? <X size={13} /> : <Plus size={13} />}
              {showNewForm ? "Cancel" : "Add account"}
            </button>
          </div>

          {showNewForm && (
            <Card>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
                <div>
                  <label className="mb-1 block text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                    Account number
                  </label>
                  <input
                    type="text"
                    value={newAccount.account_number}
                    onChange={(e) =>
                      setNewAccount((p) => ({ ...p, account_number: e.target.value }))
                    }
                    placeholder="e.g. 6200"
                    className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm font-mono"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                    Account name
                  </label>
                  <input
                    type="text"
                    value={newAccount.account_name}
                    onChange={(e) =>
                      setNewAccount((p) => ({ ...p, account_name: e.target.value }))
                    }
                    placeholder="e.g. SaaS · Productivity"
                    className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                    Account type
                  </label>
                  <select
                    value={newAccount.account_type}
                    onChange={(e) =>
                      setNewAccount((p) => ({ ...p, account_type: e.target.value }))
                    }
                    className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
                  >
                    {ACCOUNT_TYPE_ORDER.map((t) => (
                      <option key={t} value={t}>
                        {ACCOUNT_TYPE_LABELS[t]}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="flex items-end">
                  <button
                    onClick={handleCreate}
                    disabled={creating || !newAccount.account_number || !newAccount.account_name}
                    className="inline-flex items-center gap-1.5 rounded-md bg-credit px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 transition disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {creating ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                    {creating ? "Creating…" : "Create"}
                  </button>
                </div>
              </div>
            </Card>
          )}

          {accounts.length === 0 && !loading && (
            <div className="rounded-xl border border-default bg-surface p-8 text-center text-sm text-ink-tertiary">
              No accounts found.{" "}
              <button
                onClick={() => setShowNewForm(true)}
                className="text-brand-400 hover:underline"
              >
                Add the first account
              </button>
            </div>
          )}

          {ACCOUNT_TYPE_ORDER.filter((t) => grouped[t]?.length > 0).map((type) => (
            <Card key={type} padded={false}>
              <div className="border-b border-subtle px-4 md:px-6 py-3">
                <h3 className="text-sm font-semibold">{ACCOUNT_TYPE_LABELS[type]}</h3>
              </div>
              <div className="divide-y divide-subtle/60">
                {grouped[type].map((account) => (
                  <div
                    key={account.id}
                    className={cn(
                      "flex items-center gap-3 px-4 md:px-6 py-2.5 text-sm",
                      !account.is_active && "opacity-40"
                    )}
                  >
                    <span className="w-14 shrink-0 font-mono text-xs text-ink-tertiary">
                      {account.account_number}
                    </span>

                    {editingId === account.id ? (
                      <>
                        <input
                          type="text"
                          value={editName}
                          onChange={(e) => setEditName(e.target.value)}
                          className="flex-1 rounded-md border border-default bg-canvas py-1 px-2 text-sm"
                          autoFocus
                        />
                        <select
                          value={editType}
                          onChange={(e) => setEditType(e.target.value)}
                          className="w-36 rounded-md border border-default bg-canvas py-1 px-2 text-xs"
                        >
                          {ACCOUNT_TYPE_ORDER.map((t) => (
                            <option key={t} value={t}>
                              {ACCOUNT_TYPE_LABELS[t]}
                            </option>
                          ))}
                        </select>
                        <button
                          onClick={() => handleSaveEdit(account)}
                          className="rounded p-1 text-credit hover:bg-white/5"
                        >
                          <Check size={14} />
                        </button>
                        <button
                          onClick={() => setEditingId(null)}
                          className="rounded p-1 text-ink-tertiary hover:bg-white/5"
                        >
                          <X size={14} />
                        </button>
                      </>
                    ) : (
                      <>
                        <span className="flex-1 truncate">{account.account_name}</span>
                        <span className="hidden md:inline text-[10px] uppercase tracking-wider text-ink-tertiary">
                          {account.normal_balance}
                        </span>
                        {!account.is_active && (
                          <span className="rounded-full bg-ink-disabled/20 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                            Inactive
                          </span>
                        )}
                        {account.is_system && (
                          <span className="rounded-full bg-brand-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-brand-400">
                            System
                          </span>
                        )}
                        <div className="flex items-center gap-0.5">
                          <button
                            onClick={() => {
                              setEditingId(account.id);
                              setEditName(account.account_name);
                              setEditType(account.account_type);
                            }}
                            className="rounded p-1 text-ink-tertiary hover:text-ink-primary hover:bg-white/5"
                            title="Rename / reclassify"
                          >
                            <Pencil size={12} />
                          </button>
                          <button
                            onClick={() => handleDelete(account)}
                            disabled={deactivating === account.id || account.is_system || !account.is_active}
                            className="rounded p-1 text-ink-tertiary hover:text-debit hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-30"
                            title="Deactivate"
                          >
                            {deactivating === account.id ? (
                              <Loader2 size={12} className="animate-spin" />
                            ) : (
                              <Trash2 size={12} />
                            )}
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          ))}
        </>
      )}
    </div>
  );
}

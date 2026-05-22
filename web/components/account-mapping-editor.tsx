"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState, useTransition } from "react";
import { Building2, CheckCircle2, ChevronDown, Loader2, ShieldAlert } from "lucide-react";
import { cn } from "@/lib/cn";
import { formatUSD } from "@/lib/cash-position";
import type { EntityRow, LiveAccount } from "@/lib/account-mappings";

export function AccountMappingEditor({
  workspaceId,
  liveAccounts,
  entities
}: {
  workspaceId: string;
  liveAccounts: LiveAccount[];
  entities: EntityRow[];
}) {
  const router = useRouter();
  const [draft, setDraft] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const acct of liveAccounts) {
      init[acct.external_account_id] = acct.entity_id ?? "";
    }
    return init;
  });
  const [autoCategorize, setAutoCategorize] = useState<Record<string, boolean>>(() => {
    const init: Record<string, boolean> = {};
    for (const acct of liveAccounts) {
      init[acct.external_account_id] = acct.auto_categorize ?? true;
    }
    return init;
  });
  const [saving, startSaving] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<Date | null>(null);

  const dirty = useMemo(() => {
    return liveAccounts.some(
      (a) =>
        draft[a.external_account_id] !== (a.entity_id ?? "") ||
        autoCategorize[a.external_account_id] !== (a.auto_categorize ?? true)
    );
  }, [draft, autoCategorize, liveAccounts]);

  const unmappedCount = liveAccounts.filter(
    (a) => !draft[a.external_account_id]
  ).length;

  async function save() {
    setError(null);
    const mappings = liveAccounts
      .filter((a) => draft[a.external_account_id])
      .map((a) => ({
        entity_id: draft[a.external_account_id],
        source: a.source,
        external_account_id: a.external_account_id,
        external_org: a.external_org,
        external_account_name: a.external_account_name,
        auto_categorize: autoCategorize[a.external_account_id] ?? true,
        label: a.label ?? null,
        notes: null
      }));

    if (mappings.length === 0) {
      setError("Nothing to save — assign at least one account to an entity.");
      return;
    }

    startSaving(async () => {
      try {
        const res = await fetch(`/api/workspaces/${workspaceId}/account-mappings`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mappings })
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
          throw new Error(body.detail ?? "save failed");
        }
        setSavedAt(new Date());
        router.refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
      }
    });
  }

  if (liveAccounts.length === 0) {
    return (
      <div className="rounded-xl border border-default bg-surface p-6 text-center text-sm text-ink-tertiary">
        No accounts to map yet. Connect a banking aggregator first, then come back here to assign
        each account to one of your entities.
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-default bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-subtle px-5 py-4">
        <div>
          <h2 className="text-base font-semibold">Account → Entity mapping</h2>
          <p className="mt-0.5 text-xs text-ink-tertiary">
            Tell the agents which entity owns each account so EntitySelector
            filters correctly and bookkeeping rules apply to the right books.
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs">
          {unmappedCount > 0 ? (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-pending/15 px-2.5 py-1 text-pending">
              <ShieldAlert size={12} />
              {unmappedCount} unmapped
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-credit/15 px-2.5 py-1 text-credit">
              <CheckCircle2 size={12} />
              All accounts mapped
            </span>
          )}
        </div>
      </div>

      <ul className="divide-y divide-subtle/60">
        {liveAccounts.map((acct) => {
          const balance = parseFloat(acct.balance);
          const isMapped = !!draft[acct.external_account_id];
          return (
            <li
              key={acct.external_account_id}
              className={cn(
                "grid grid-cols-1 gap-3 px-5 py-4 md:grid-cols-12",
                !isMapped && "bg-pending/[0.025]"
              )}
            >
              <div className="md:col-span-5">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <Building2 size={13} className="text-ink-tertiary" />
                  {acct.external_account_name}
                </div>
                <div className="mt-0.5 text-[10px] uppercase tracking-wider text-ink-tertiary">
                  {acct.external_org} ·{" "}
                  <span className="font-mono">{acct.external_account_id}</span>
                </div>
                <div
                  className={cn(
                    "mt-1 font-mono text-xs tabular-nums",
                    balance >= 0 ? "text-credit" : "text-debit"
                  )}
                >
                  {balance >= 0 ? "+" : "-"}
                  {formatUSD(Math.abs(balance))} {acct.currency}
                </div>
              </div>

              <div className="md:col-span-5">
                <label className="block text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                  Owning entity
                </label>
                <div className="relative mt-1">
                  <select
                    value={draft[acct.external_account_id]}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        [acct.external_account_id]: e.target.value
                      })
                    }
                    className="w-full appearance-none rounded-md border border-default bg-canvas py-1.5 pl-3 pr-8 text-sm focus:border-brand-500 focus:outline-none"
                  >
                    <option value="">— unmapped —</option>
                    {entities.map((e) => (
                      <option key={e.id} value={e.id}>
                        {e.display_name} ({e.kind})
                      </option>
                    ))}
                  </select>
                  <ChevronDown
                    size={13}
                    className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-ink-tertiary"
                  />
                </div>
              </div>

              <div className="md:col-span-2 md:text-right">
                <label className="inline-flex cursor-pointer items-center gap-1.5 text-[11px] text-ink-secondary">
                  <input
                    type="checkbox"
                    className="h-3 w-3 accent-brand-500"
                    checked={autoCategorize[acct.external_account_id] ?? true}
                    onChange={(e) =>
                      setAutoCategorize({
                        ...autoCategorize,
                        [acct.external_account_id]: e.target.checked
                      })
                    }
                  />
                  Auto-categorize
                </label>
              </div>
            </li>
          );
        })}
      </ul>

      {(error || savedAt) && (
        <div className="border-t border-subtle px-5 py-2.5 text-xs">
          {error && <span className="text-debit">{error}</span>}
          {savedAt && !error && (
            <span className="text-credit">
              Saved {savedAt.toLocaleTimeString("en-US", { hour12: false })}.
            </span>
          )}
        </div>
      )}

      <div className="flex items-center justify-end gap-2 border-t border-subtle bg-surface/40 px-5 py-3">
        <button
          onClick={save}
          disabled={!dirty || saving}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-4 py-1.5 text-sm font-medium text-white shadow-brand-glow hover:bg-brand-600 transition",
            (!dirty || saving) && "cursor-not-allowed opacity-60"
          )}
        >
          {saving ? <Loader2 size={13} className="animate-spin" /> : null}
          {saving ? "Saving..." : dirty ? "Save mappings" : "No changes"}
        </button>
      </div>
    </div>
  );
}

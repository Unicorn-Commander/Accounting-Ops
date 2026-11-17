"use client";

// Search-and-pick for one journal entry of THIS workspace. It lists entries
// through the existing workspace-scoped proxy
// (web/app/api/workspaces/[workspaceId]/journal-entries GET), so only entries
// the backend returns for this workspace (and the caller's entity scope) can
// be picked; there is no free-text id field.

import { useState } from "react";
import { Loader2, Search } from "lucide-react";
import type { FetchLike } from "@/lib/api-error";

export type PickedJournalEntry = {
  id: string;
  effective_date: string;
  amount: string;
  currency_code: string;
  description: string | null;
  payee: string | null;
  entity_display_name: string | null;
};

export function journalSearchUrl(workspaceId: string, query: string, entityId: string | null): string {
  const params = new URLSearchParams();
  if (query.trim()) params.set("search", query.trim());
  if (entityId) params.set("entity_id", entityId);
  params.set("status_filter", "posted");
  params.set("limit", "20");
  return `/api/workspaces/${encodeURIComponent(workspaceId)}/journal-entries?${params}`;
}

export function JournalEntryPicker({
  workspaceId,
  entityId,
  value,
  onChange,
  fetchImpl,
}: {
  workspaceId: string;
  entityId: string | null;
  value: PickedJournalEntry | null;
  onChange: (entry: PickedJournalEntry | null) => void;
  fetchImpl?: FetchLike;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<PickedJournalEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function search() {
    setLoading(true);
    setError(null);
    try {
      const res = await (fetchImpl ?? fetch)(journalSearchUrl(workspaceId, query, entityId), {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = (await res.json()) as { entries?: PickedJournalEntry[] };
      setResults(Array.isArray(body.entries) ? body.entries : []);
    } catch {
      setError("Could not load journal entries. Try again.");
      setResults(null);
    } finally {
      setLoading(false);
    }
  }

  if (value) {
    return (
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-subtle bg-canvas/40 p-2 text-[12px]">
        <span>
          <span className="font-mono">{value.effective_date}</span>{" "}
          <span className="font-mono">{value.amount} {value.currency_code}</span>{" "}
          {value.payee || value.description || ""}
          <span className="ml-2 font-mono text-[10px] text-ink-tertiary">{value.id}</span>
        </span>
        <button type="button" onClick={() => onChange(null)} className="text-[11px] text-brand-400 hover:underline">
          Change
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-2">
        <input
          type="search"
          aria-label="Search journal entries"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void search();
            }
          }}
          placeholder="Search payee, description, memo or account"
          className="h-8 flex-1 rounded-md border border-subtle bg-surface px-2 text-[12px]"
        />
        <button
          type="button"
          onClick={() => void search()}
          disabled={loading}
          className="inline-flex h-8 items-center gap-1 rounded-md border border-subtle bg-surface px-3 text-[12px] hover:bg-elevated disabled:opacity-50"
        >
          {loading ? <Loader2 size={12} className="animate-spin" /> : <Search size={12} />}
          Search
        </button>
      </div>
      {error && <p role="alert" className="text-[11px] text-debit">{error}</p>}
      {results && results.length === 0 && <p className="text-[11px] text-ink-tertiary">No matching journal entries in this workspace.</p>}
      {results && results.length > 0 && (
        <ul className="max-h-56 overflow-auto rounded-md border border-subtle text-[12px]">
          {results.map((entry) => (
            <li key={entry.id}>
              <button
                type="button"
                onClick={() => onChange(entry)}
                className="flex w-full flex-wrap items-center gap-2 px-2 py-1.5 text-left hover:bg-elevated"
              >
                <span className="font-mono">{entry.effective_date}</span>
                <span className="font-mono">{entry.amount} {entry.currency_code}</span>
                <span className="truncate text-ink-secondary">{entry.payee || entry.description || "(no description)"}</span>
                {entry.entity_display_name && <span className="text-[10px] text-ink-tertiary">{entry.entity_display_name}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

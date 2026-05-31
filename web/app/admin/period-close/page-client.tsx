"use client";

import { useMemo, useState } from "react";
import { AlertTriangle, Check, Lock, RefreshCw } from "lucide-react";
import { Card } from "@/components/primitives";
import type { Entity } from "@/lib/api";
import type {
  ClosedPeriod,
  ClosedPeriodsResponse,
  ClosePeriodResponse,
} from "@/lib/reports-trial-balance";

type PeriodMap = Record<string, ClosedPeriod[]>;

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

async function readError(res: Response): Promise<string> {
  const data = await res.json().catch(() => null);
  if (data?.detail) return String(data.detail);
  if (data?.error) return String(data.error);
  return `${res.status} ${res.statusText}`;
}

export function PeriodCloseClient({
  workspaceId,
  entities,
  initialClosedPeriods,
}: {
  workspaceId: string;
  entities: Entity[];
  initialClosedPeriods: { entityId: string; periods: ClosedPeriod[] }[];
}) {
  const initialMap = useMemo(
    () =>
      Object.fromEntries(
        initialClosedPeriods.map((item) => [item.entityId, item.periods])
      ) as PeriodMap,
    [initialClosedPeriods]
  );
  const [periodsByEntity, setPeriodsByEntity] = useState<PeriodMap>(initialMap);
  const [entityId, setEntityId] = useState(entities[0]?.id ?? "");
  const [periodEnd, setPeriodEnd] = useState("");
  const [reason, setReason] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [refreshing, setRefreshing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [lastClose, setLastClose] = useState<ClosePeriodResponse | null>(null);
  const selectedEntity = entities.find((entity) => entity.id === entityId) ?? entities[0];

  async function refreshEntity(nextEntityId: string) {
    setRefreshing(nextEntityId);
    setError(null);
    try {
      const res = await fetch(
        `/api/workspaces/${workspaceId}/entities/${nextEntityId}/closed-periods`
      );
      if (!res.ok) throw new Error(await readError(res));
      const data = (await res.json()) as ClosedPeriodsResponse;
      setPeriodsByEntity((prev) => ({
        ...prev,
        [nextEntityId]: data.closed_periods,
      }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshing(null);
    }
  }

  async function closeSelectedPeriod() {
    if (!selectedEntity || !periodEnd) return;
    setSubmitting(true);
    setError(null);
    setToast(null);
    setLastClose(null);
    try {
      const res = await fetch(
        `/api/workspaces/${workspaceId}/entities/${selectedEntity.id}/close-period`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            period_end: periodEnd,
            reason: reason.trim() || null,
          }),
        }
      );
      if (!res.ok) throw new Error(await readError(res));
      const data = (await res.json()) as ClosePeriodResponse;
      setLastClose(data);
      setConfirmOpen(false);
      setPeriodEnd("");
      setReason("");
      setToast(
        `Closed ${data.closed_period.period_end}; ${data.entries_in_period} posted entries were already in period.`
      );
      await refreshEntity(selectedEntity.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-5">
      {toast ? (
        <div className="rounded-lg border border-credit/30 bg-credit/[0.06] px-4 py-3 text-sm text-credit">
          <span className="inline-flex items-center gap-2">
            <Check size={16} />
            {toast}
          </span>
        </div>
      ) : null}
      {error ? (
        <div className="rounded-lg border border-debit/30 bg-debit/[0.06] px-4 py-3 text-sm text-debit">
          {error}
        </div>
      ) : null}

      <Card>
        <h2 className="text-base font-semibold">Close a new period</h2>
        <form
          className="mt-4 grid gap-4 md:grid-cols-[minmax(220px,1fr)_180px] md:items-end"
          onSubmit={(event) => {
            event.preventDefault();
            setConfirmOpen(true);
          }}
        >
          {entities.length > 1 ? (
            <label className="grid gap-1.5 text-xs text-ink-secondary">
              <span className="font-medium uppercase tracking-wider text-ink-tertiary">
                Entity
              </span>
              <select
                value={entityId}
                onChange={(event) => setEntityId(event.target.value)}
                className="h-9 rounded-md border border-default bg-canvas px-3 text-sm text-ink-primary outline-none focus:border-brand-500"
              >
                {entities.map((entity) => (
                  <option key={entity.id} value={entity.id}>
                    {entity.display_name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <label className="grid gap-1.5 text-xs text-ink-secondary">
            <span className="font-medium uppercase tracking-wider text-ink-tertiary">
              Period end
            </span>
            <input
              type="date"
              required
              value={periodEnd}
              onChange={(event) => setPeriodEnd(event.target.value)}
              className="h-9 rounded-md border border-default bg-canvas px-3 text-sm text-ink-primary outline-none focus:border-brand-500"
            />
          </label>
          <label className="grid gap-1.5 text-xs text-ink-secondary md:col-span-2">
            <span className="font-medium uppercase tracking-wider text-ink-tertiary">
              Reason
            </span>
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={3}
              className="rounded-md border border-default bg-canvas px-3 py-2 text-sm text-ink-primary outline-none focus:border-brand-500"
              placeholder="Books reconciled, return filed, or other close note."
            />
          </label>
          <div className="md:col-span-2">
            <button
              type="submit"
              disabled={!periodEnd || !entityId}
              className="inline-flex h-9 items-center gap-1.5 rounded-md bg-brand-500 px-3 text-sm font-medium text-white shadow-brand-glow hover:bg-brand-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Lock size={14} />
              Close period
            </button>
          </div>
        </form>
      </Card>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {entities.map((entity) => {
          const periods = periodsByEntity[entity.id] ?? [];
          return (
            <Card key={entity.id} padded={false}>
              <div className="flex items-center justify-between gap-3 border-b border-subtle px-5 py-3">
                <div>
                  <h2 className="text-sm font-semibold">{entity.display_name}</h2>
                  <p className="text-xs text-ink-tertiary">{entity.legal_name ?? entity.slug}</p>
                </div>
                <button
                  type="button"
                  onClick={() => refreshEntity(entity.id)}
                  className="inline-flex h-8 items-center gap-1.5 rounded-md border border-default bg-surface px-2.5 text-xs text-ink-secondary hover:bg-elevated"
                >
                  <RefreshCw size={12} />
                  {refreshing === entity.id ? "Refreshing" : "Refresh"}
                </button>
              </div>
              {periods.length === 0 ? (
                <p className="px-5 py-4 text-sm text-ink-tertiary">
                  No closed periods.
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[620px] text-sm">
                    <thead>
                      <tr className="border-b border-subtle text-left text-[10px] uppercase tracking-wider text-ink-tertiary">
                        <th className="px-5 py-2.5 font-medium">Period end</th>
                        <th className="px-5 py-2.5 font-medium">Closed at</th>
                        <th className="px-5 py-2.5 font-medium">Closed by</th>
                        <th className="px-5 py-2.5 font-medium">Reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {periods.map((period) => (
                        <tr
                          key={`${entity.id}-${period.period_end}`}
                          className="border-b border-subtle/40 last:border-b-0"
                        >
                          <td className="px-5 py-2.5 font-mono text-xs">
                            {period.period_end}
                          </td>
                          <td className="px-5 py-2.5 text-xs text-ink-secondary">
                            {formatDateTime(period.closed_at)}
                          </td>
                          <td className="px-5 py-2.5 font-mono text-xs">
                            {period.closed_by_uc_uid}
                          </td>
                          <td className="px-5 py-2.5 text-xs text-ink-secondary">
                            {period.reason ?? "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          );
        })}
      </div>

      {confirmOpen && selectedEntity ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/85 px-4 backdrop-blur-sm">
          <div className="w-full max-w-lg rounded-lg border border-default bg-surface p-5 shadow-card">
            <div className="flex items-start gap-3">
              <div className="mt-0.5 rounded-full bg-debit/15 p-2 text-debit">
                <AlertTriangle size={18} />
              </div>
              <div>
                <h2 className="text-base font-semibold">Confirm period close</h2>
                <p className="mt-2 text-sm text-ink-secondary">
                  This will lock postings dated on or before {periodEnd} for{" "}
                  {selectedEntity.display_name}. There is no reopen button by design.
                </p>
                {lastClose ? (
                  <p className="mt-2 text-sm text-ink-secondary">
                    Last close returned {lastClose.entries_in_period} posted entries
                    in the period.
                  </p>
                ) : null}
              </div>
            </div>
            <div className="mt-5 flex flex-wrap justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmOpen(false)}
                disabled={submitting}
                className="inline-flex h-9 items-center rounded-md border border-default bg-surface px-3 text-sm text-ink-secondary hover:bg-elevated disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={closeSelectedPeriod}
                disabled={submitting}
                className="inline-flex h-9 items-center gap-1.5 rounded-md bg-debit px-3 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
              >
                <Lock size={14} />
                {submitting ? "Closing" : "I understand - close"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

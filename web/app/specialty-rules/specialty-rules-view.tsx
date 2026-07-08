"use client";

// SpecialtyRulesView — wired view of compliance rules across the workspace.
//
// Replaces the previous static RULES array. Backed by
// GET /api/workspaces/{wid}/specialty-rules + PATCH for deactivate/reactivate.
//
// Authoring new rules still flows through the agent proposal pipeline
// (POST /entities/{eid}/specialty-rules) — the page surfaces a "Talk to
// the Compliance agent" call-to-action that opens /chat with the right
// persona instead of inventing a new manual write path.

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import Link from "next/link";
import { AlertCircle, MessageSquare, Pause, Play, Plus, Settings } from "lucide-react";
import { Card, StatusChip } from "@/components/primitives";
import { Button } from "@/components/ui/button";
import type { SpecialtyRuleRow, SpecialtyRulesResponse } from "@/lib/specialty-rules";

type Role =
  | "platform_admin"
  | "owner"
  | "client_owner"
  | "admin"
  | "cpa"
  | "accountant"
  | "bookkeeper"
  | "client_user"
  | "viewer";

const WRITE_ROLES: ReadonlySet<Role> = new Set([
  "platform_admin",
  "owner",
  "client_owner",
  "admin",
  "cpa",
  "accountant"
]);

export function SpecialtyRulesView({
  workspaceId,
  initial,
  includeInactive,
  role
}: {
  workspaceId: string;
  initial: SpecialtyRulesResponse | null;
  includeInactive: boolean;
  role: Role | null;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [data, setData] = useState<SpecialtyRulesResponse | null>(initial);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "active" | "inactive">(
    includeInactive ? "all" : "active"
  );

  const canEdit = role != null && WRITE_ROLES.has(role);

  const toggle = async (rule: SpecialtyRuleRow) => {
    if (!canEdit) return;
    setBusyId(rule.id);
    setError(null);
    try {
      const res = await fetch(
        `/api/workspaces/${workspaceId}/specialty-rules/${encodeURIComponent(
          rule.id
        )}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action: rule.active ? "deactivate" : "reactivate"
          })
        }
      );
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
        throw new Error(
          (body.detail as string) ?? `Request failed (HTTP ${res.status})`
        );
      }
      // Re-fetch list (small page; full reload is cheaper than reconciling).
      const refreshed = await fetch(
        `/api/workspaces/${workspaceId}/specialty-rules${
          filter === "all" ? "?include_inactive=true" : ""
        }`,
        { cache: "no-store" }
      );
      if (refreshed.ok) {
        setData((await refreshed.json()) as SpecialtyRulesResponse);
      }
      startTransition(() => router.refresh());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const onFilterChange = async (next: "all" | "active" | "inactive") => {
    setFilter(next);
    try {
      const qs = next === "all" ? "?include_inactive=true" : "";
      const res = await fetch(
        `/api/workspaces/${workspaceId}/specialty-rules${qs}`,
        { cache: "no-store" }
      );
      if (res.ok) {
        const json = (await res.json()) as SpecialtyRulesResponse;
        if (next === "inactive") {
          json.rows = json.rows.filter((r) => !r.active);
        }
        setData(json);
      }
    } catch {
      // Best-effort.
    }
  };

  const rows = data?.rows ?? [];
  const total = data?.total ?? 0;

  return (
    <div>
      {/* Toolbar row */}
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-1.5">
          {(["active", "all", "inactive"] as const).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => onFilterChange(f)}
              className={
                "rounded-md border px-3 py-1 text-xs font-medium transition " +
                (filter === f
                  ? "border-brand-500 bg-brand-500/10 text-brand-300"
                  : "border-default bg-surface text-ink-secondary hover:bg-elevated")
              }
            >
              {f === "active" ? "Active" : f === "all" ? "All (incl. paused)" : "Paused / expired"}
            </button>
          ))}
        </div>
        {canEdit ? (
          <Link
            href="/chat?persona=compliance-rule-author"
            className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-brand-600"
            title="New rules route through the Compliance agent so the proposal is audited end-to-end."
          >
            <Plus size={13} /> Propose new rule
          </Link>
        ) : null}
      </div>

      {error ? (
        <Card className="mb-3 border-debit/40 bg-debit/[0.05]">
          <p className="flex items-center gap-1.5 text-xs text-debit">
            <AlertCircle size={12} /> {error}
          </p>
        </Card>
      ) : null}

      {data === null ? (
        <Card>
          <p className="text-sm text-ink-tertiary">
            Specialty-rules service unavailable. Try refreshing in a moment.
          </p>
        </Card>
      ) : rows.length === 0 ? (
        <Card>
          <p className="text-sm text-ink-secondary">
            No rules{filter === "active" ? " active" : ""} for the selected scope.
          </p>
          {canEdit ? (
            <p className="mt-2 text-xs text-ink-tertiary">
              Talk to the Compliance agent in{" "}
              <Link
                href="/chat?persona=compliance-rule-author"
                className="text-brand-400 underline-offset-2 hover:underline"
              >
                /chat
              </Link>{" "}
              to draft your first rule — every addition queues for human
              approval so the audit trail is preserved.
            </p>
          ) : null}
        </Card>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {rows.map((r) => {
            const isBusy = busyId === r.id && pending;
            return (
              <Card key={r.id} className={r.active ? "" : "opacity-70"}>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="text-base font-semibold">{r.kind}</h3>
                      {r.active ? (
                        <StatusChip tone="credit">Active</StatusChip>
                      ) : (
                        <StatusChip tone="ink">Paused</StatusChip>
                      )}
                    </div>
                    {r.entity_display_name ? (
                      <p className="mt-0.5 text-[11px] text-ink-tertiary">
                        Entity:{" "}
                        <span className="text-ink-secondary">
                          {r.entity_display_name}
                        </span>
                      </p>
                    ) : null}
                  </div>
                  {canEdit ? (
                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => toggle(r)}
                      className="inline-flex h-7 w-7 items-center justify-center rounded-md text-ink-tertiary hover:bg-white/5 hover:text-ink-primary disabled:opacity-50"
                      title={r.active ? "Pause rule" : "Reactivate rule"}
                    >
                      {r.active ? <Pause size={13} /> : <Play size={13} />}
                    </button>
                  ) : null}
                </div>

                <p className="mt-3 text-sm leading-relaxed text-ink-secondary">
                  {r.reasoning || "(no reasoning recorded)"}
                </p>

                <dl className="mt-4 grid grid-cols-3 gap-3 text-[11px]">
                  <div>
                    <dt className="uppercase tracking-wider text-ink-tertiary">
                      Trigger
                    </dt>
                    <dd className="mt-0.5 font-mono">{r.trigger}</dd>
                  </div>
                  <div>
                    <dt className="uppercase tracking-wider text-ink-tertiary">
                      Action
                    </dt>
                    <dd className="mt-0.5 font-mono">{r.action}</dd>
                  </div>
                  <div>
                    <dt className="uppercase tracking-wider text-ink-tertiary">
                      Hits / YTD
                    </dt>
                    <dd className="mt-0.5 font-mono">{r.hits_ytd}</dd>
                  </div>
                </dl>

                <div className="mt-4 flex items-center justify-between text-[11px] text-ink-tertiary">
                  <span>
                    Source: {r.source.replace(/_/g, " ") || "—"} · Profile:{" "}
                    {r.profile_name || "—"}
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <Settings size={11} />{" "}
                    {r.effective_to
                      ? `expires ${new Date(r.effective_to).toLocaleDateString()}`
                      : "no expiry"}
                  </span>
                </div>
              </Card>
            );
          })}
        </div>
      )}

      <p className="mt-6 flex items-center gap-2 text-xs text-ink-tertiary">
        <MessageSquare size={12} />
        Want a rule we don&apos;t ship? Describe it in{" "}
        <Link
          href="/chat"
          className="text-brand-400 underline-offset-2 hover:underline"
        >
          /chat
        </Link>{" "}
        — the Compliance agent drafts a proposal for your review.
        {data ? (
          <span className="ml-auto text-ink-tertiary">
            {total} rule{total === 1 ? "" : "s"} · as of{" "}
            {new Date(data.as_of).toLocaleString()}
          </span>
        ) : null}
      </p>
    </div>
  );
}

export const _WRITE_ROLES = WRITE_ROLES;

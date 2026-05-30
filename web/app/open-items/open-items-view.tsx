"use client";

// OpenItemsView — interactive surface for the /open-items dashboard.
//
// Renders six sections:
//   1. Time-sensitive       (red / orange treatment)
//   2. Documents Aaron needs to provide
//   3. Decisions awaiting Aaron
//   4. Items waiting on Mosin (CPA) — read-only
//   5. Items waiting on attorney    — read-only
//   6. Resolved this week           — rollup of wins
//
// State that mutates (mark-doc-received, decision verdict) is persisted via
// fire-and-forget POSTs to /api/workspaces/{wid}/open-items/* and a
// router.refresh() afterwards so the next render shows the truth.

import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useState, useTransition } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  Calendar,
  CheckCircle2,
  ChevronRight,
  Clock,
  ExternalLink,
  FileText,
  Gavel,
  Hourglass,
  PartyPopper,
  Scale,
  Sparkles,
  User
} from "lucide-react";
import { Card, StatusChip } from "@/components/primitives";
import { cn } from "@/lib/cn";
import type {
  DecisionItem,
  DocChecklistItem,
  OpenItemsResponse,
  QueueRow,
  ResolvedRow
} from "@/lib/open-items";

// ---------------------------------------------------------------------------
// Top KPI strip
// ---------------------------------------------------------------------------

function CountTile({
  label,
  value,
  tone = "ink",
  href
}: {
  label: string;
  value: number;
  tone?: "ink" | "debit" | "pending" | "brand" | "credit";
  href?: string;
}) {
  const toneCls: Record<string, string> = {
    ink: "text-ink-primary",
    debit: "text-debit",
    pending: "text-pending",
    brand: "text-brand-400",
    credit: "text-credit"
  };
  const borderCls: Record<string, string> = {
    ink: "border-default",
    debit: "border-debit/40",
    pending: "border-pending/40",
    brand: "border-brand-500/30",
    credit: "border-credit/30"
  };
  const inner = (
    <div
      className={cn(
        "rounded-xl border bg-surface px-4 py-3 transition hover:bg-white/[0.02]",
        borderCls[tone]
      )}
    >
      <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
        {label}
      </p>
      <p className={cn("mt-1 font-mono text-2xl tabular-nums", toneCls[tone])}>
        {value}
      </p>
    </div>
  );
  if (href) {
    return (
      <a href={href} className="block">
        {inner}
      </a>
    );
  }
  return inner;
}

// ---------------------------------------------------------------------------
// Year filter pills
// ---------------------------------------------------------------------------

function YearFilter({
  currentTaxYear
}: {
  currentTaxYear: number | null;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const switchYear = (year: number | null) => {
    const sp = new URLSearchParams(params.toString());
    if (year === null) sp.delete("tax_year");
    else sp.set("tax_year", String(year));
    router.push(`${pathname}${sp.toString() ? "?" + sp.toString() : ""}`);
  };

  const years = [2025, 2024, 2023, 2022] as const;

  return (
    <div className="flex flex-wrap items-center gap-1">
      <span className="mr-2 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
        Tax year
      </span>
      <button
        type="button"
        onClick={() => switchYear(null)}
        className={cn(
          "rounded-md px-2.5 py-1 text-xs transition",
          currentTaxYear === null
            ? "bg-brand-500/15 text-brand-300"
            : "bg-white/[0.03] text-ink-secondary hover:bg-white/[0.06]"
        )}
      >
        All
      </button>
      {years.map((y) => (
        <button
          type="button"
          key={y}
          onClick={() => switchYear(y)}
          className={cn(
            "rounded-md px-2.5 py-1 text-xs font-mono transition",
            currentTaxYear === y
              ? "bg-brand-500/15 text-brand-300"
              : "bg-white/[0.03] text-ink-secondary hover:bg-white/[0.06]"
          )}
        >
          {y}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section wrapper
// ---------------------------------------------------------------------------

function SectionHeading({
  icon,
  title,
  count,
  tone = "ink",
  subtitle
}: {
  icon: React.ReactNode;
  title: string;
  count: number;
  tone?: "ink" | "debit" | "pending" | "brand" | "credit" | "agent";
  subtitle?: string;
}) {
  const toneCls: Record<string, string> = {
    ink: "text-ink-primary",
    debit: "text-debit",
    pending: "text-pending",
    brand: "text-brand-400",
    credit: "text-credit",
    agent: "text-agent-500"
  };
  return (
    <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
      <div className="flex items-center gap-2.5">
        <span className={cn("flex h-7 w-7 items-center justify-center rounded-lg bg-white/[0.04]", toneCls[tone])}>
          {icon}
        </span>
        <div>
          <h2 className="text-base font-semibold leading-tight">
            {title}
            <span className={cn("ml-2 font-mono text-sm", toneCls[tone])}>
              {count}
            </span>
          </h2>
          {subtitle ? (
            <p className="mt-0.5 text-[11px] text-ink-tertiary">{subtitle}</p>
          ) : null}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section 1 — Time-sensitive
// ---------------------------------------------------------------------------

function TimeSensitiveSection({ rows }: { rows: QueueRow[] }) {
  if (rows.length === 0) {
    return (
      <Card className="border-credit/20 bg-credit/[0.03]">
        <div className="flex items-center gap-2 text-sm text-credit">
          <CheckCircle2 size={15} />
          <span>No time-sensitive items right now.</span>
        </div>
      </Card>
    );
  }
  return (
    <div className="space-y-2.5">
      {rows.map((r) => (
        <Card
          key={r.id}
          className="border-debit/40 bg-debit/[0.05] hover:border-debit/60 transition"
          padded={false}
        >
          <div className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-start sm:gap-4 sm:px-5 sm:py-3.5">
            <AlertTriangle
              size={16}
              className="mt-0.5 shrink-0 text-debit"
            />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold text-debit">
                  {r.form_reference || "Time-sensitive item"}
                </span>
                <span className="rounded-full bg-debit/10 px-2 py-0.5 text-[10px] font-mono font-medium text-debit">
                  TY {r.tax_year}
                </span>
                {r.entity_display_name ? (
                  <span className="text-[11px] text-ink-tertiary">
                    {r.entity_display_name}
                  </span>
                ) : null}
              </div>
              {r.summary ? (
                <p className="mt-1.5 text-xs leading-relaxed text-ink-secondary line-clamp-3">
                  {r.summary}
                </p>
              ) : null}
              <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-ink-tertiary">
                <span className="inline-flex items-center gap-1">
                  <Sparkles size={11} /> {r.submitter_id}
                </span>
                <Link
                  href={`/taxes/${r.tax_year}?entity_id=${r.entity_id}`}
                  className="inline-flex items-center gap-0.5 text-debit/80 underline-offset-2 hover:underline"
                >
                  Open in /taxes/{r.tax_year}
                  <ChevronRight size={11} />
                </Link>
              </div>
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section 2 — Documents
// ---------------------------------------------------------------------------

const PRIORITY_CHIP: Record<DocChecklistItem["priority"], { tone: string; label: string }> = {
  time_sensitive: { tone: "bg-debit/15 text-debit", label: "Urgent" },
  high: { tone: "bg-pending/15 text-pending", label: "High" },
  medium: { tone: "bg-white/[0.06] text-ink-secondary", label: "Medium" },
  low: { tone: "bg-white/[0.04] text-ink-tertiary", label: "Low" }
};

function DocumentsSection({
  documents,
  workspaceId
}: {
  documents: DocChecklistItem[];
  workspaceId: string;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const toggle = async (doc: DocChecklistItem) => {
    setBusyKey(doc.key);
    try {
      await fetch(
        `/api/workspaces/${workspaceId}/open-items/document-received`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            key: doc.key,
            received: doc.status !== "received"
          })
        }
      );
      startTransition(() => router.refresh());
    } finally {
      setBusyKey(null);
    }
  };

  if (documents.length === 0) {
    return (
      <Card>
        <p className="text-sm text-ink-tertiary">
          No documents required for the selected filter.
        </p>
      </Card>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
      {documents.map((d) => {
        const isReceived = d.status === "received";
        const isBusy = busyKey === d.key && pending;
        const chip = PRIORITY_CHIP[d.priority];
        return (
          <Card
            key={d.key}
            padded={false}
            className={cn(
              "transition",
              isReceived
                ? "border-credit/40 bg-credit/[0.04]"
                : "hover:border-strong"
            )}
          >
            <div className="px-4 py-3.5">
              <div className="flex items-start gap-2.5">
                <button
                  type="button"
                  disabled={isBusy}
                  onClick={() => toggle(d)}
                  aria-label={
                    isReceived
                      ? `Mark ${d.name} as not received`
                      : `Mark ${d.name} as received`
                  }
                  className={cn(
                    "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md border transition",
                    isReceived
                      ? "border-credit bg-credit/20 text-credit"
                      : "border-strong bg-white/[0.02] text-ink-tertiary hover:border-brand-500/60 hover:text-brand-400",
                    isBusy && "opacity-50"
                  )}
                >
                  {isReceived ? <CheckCircle2 size={13} /> : null}
                </button>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span
                      className={cn(
                        "text-sm font-semibold",
                        isReceived
                          ? "text-ink-secondary line-through decoration-credit/60"
                          : "text-ink-primary"
                      )}
                    >
                      {d.name}
                    </span>
                    <span
                      className={cn(
                        "rounded-full px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider",
                        chip.tone
                      )}
                    >
                      {chip.label}
                    </span>
                  </div>
                  <p className="mt-1.5 text-[11px] leading-relaxed text-ink-tertiary line-clamp-3">
                    {d.description}
                  </p>
                  <dl className="mt-2.5 space-y-1 text-[11px]">
                    <div className="flex items-start gap-1.5">
                      <dt className="shrink-0 text-ink-tertiary">Where:</dt>
                      <dd className="min-w-0 flex-1">
                        {d.where_to_get_url ? (
                          <a
                            href={d.where_to_get_url}
                            target="_blank"
                            rel="noreferrer noopener"
                            className="inline-flex items-center gap-0.5 text-brand-400 underline-offset-2 hover:underline"
                          >
                            {d.where_to_get}
                            <ExternalLink size={10} />
                          </a>
                        ) : (
                          <span className="text-ink-secondary">
                            {d.where_to_get}
                          </span>
                        )}
                      </dd>
                    </div>
                    <div className="flex items-start gap-1.5">
                      <dt className="shrink-0 text-ink-tertiary">Needed for:</dt>
                      <dd className="min-w-0 flex-1 text-ink-secondary">
                        {d.needed_for}
                      </dd>
                    </div>
                    {d.tax_years.length > 0 && (
                      <div className="flex items-start gap-1.5">
                        <dt className="shrink-0 text-ink-tertiary">Years:</dt>
                        <dd className="min-w-0 flex-1 font-mono text-ink-secondary">
                          {d.tax_years.join(", ")}
                        </dd>
                      </div>
                    )}
                  </dl>
                  {isReceived && d.received_at && (
                    <p className="mt-2 text-[10px] text-credit/80">
                      Received {new Date(d.received_at).toLocaleDateString()}
                    </p>
                  )}
                </div>
              </div>
            </div>
          </Card>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section 3 — Decisions
// ---------------------------------------------------------------------------

function DecisionsSection({
  decisions,
  workspaceId
}: {
  decisions: DecisionItem[];
  workspaceId: string;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const record = async (
    decision: DecisionItem,
    verdict: "accept" | "other",
    notes?: string
  ) => {
    setBusyKey(decision.key);
    try {
      await fetch(`/api/workspaces/${workspaceId}/open-items/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: decision.key, verdict, notes })
      });
      startTransition(() => router.refresh());
    } finally {
      setBusyKey(null);
    }
  };

  if (decisions.length === 0) {
    return (
      <Card>
        <p className="text-sm text-ink-tertiary">
          No decisions awaiting you for the selected filter.
        </p>
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      {decisions.map((dec) => {
        const isDecided = dec.status === "resolved";
        const isBusy = busyKey === dec.key && pending;
        return (
          <Card
            key={dec.key}
            padded={false}
            className={cn(
              "transition",
              isDecided
                ? dec.verdict === "accept"
                  ? "border-credit/40 bg-credit/[0.03]"
                  : "border-brand-500/30 bg-brand-500/[0.03]"
                : "border-brand-500/20 hover:border-brand-500/40"
            )}
          >
            <div className="px-4 py-3.5 sm:px-5 sm:py-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-ink-primary">
                    {dec.question}
                  </p>
                  <p className="mt-2 text-[11px] uppercase tracking-wider text-ink-tertiary font-medium">
                    Recommendation
                  </p>
                  <p className="mt-0.5 text-sm font-medium text-brand-300">
                    {dec.recommendation}
                  </p>
                  <p className="mt-2 text-xs leading-relaxed text-ink-secondary">
                    {dec.rationale}
                  </p>
                  <p className="mt-2 text-[11px] text-ink-tertiary font-mono">
                    Applies to TY {dec.tax_years.join(", ")}
                  </p>
                </div>
                {isDecided ? (
                  <div className="shrink-0">
                    <StatusChip
                      tone={dec.verdict === "accept" ? "credit" : "agent"}
                    >
                      {dec.verdict === "accept" ? "Accepted" : "Other choice"}
                    </StatusChip>
                  </div>
                ) : null}
              </div>

              <div className="mt-4 flex flex-wrap items-center gap-2">
                {!isDecided ? (
                  <>
                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => record(dec, "accept")}
                      className={cn(
                        "rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white shadow-brand-glow transition hover:bg-brand-600",
                        isBusy && "opacity-60"
                      )}
                    >
                      Accept recommendation
                    </button>
                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => {
                        const note = window.prompt(
                          `Different choice for: ${dec.question}\n\nDescribe your decision (optional):`,
                          ""
                        );
                        if (note !== null) {
                          record(dec, "other", note || undefined);
                        }
                      }}
                      className={cn(
                        "rounded-md border border-strong bg-white/[0.03] px-3 py-1.5 text-xs font-medium text-ink-secondary transition hover:bg-white/[0.06] hover:text-ink-primary",
                        isBusy && "opacity-60"
                      )}
                    >
                      Different choice…
                    </button>
                  </>
                ) : (
                  <>
                    <span className="text-[11px] text-ink-tertiary">
                      Decided{" "}
                      {dec.resolved_at
                        ? new Date(dec.resolved_at).toLocaleString(undefined, {
                            dateStyle: "short",
                            timeStyle: "short"
                          })
                        : ""}
                    </span>
                    {dec.notes ? (
                      <span className="text-[11px] text-ink-secondary">
                        — {dec.notes}
                      </span>
                    ) : null}
                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => record(dec, "accept")}
                      className="ml-auto text-[11px] text-ink-tertiary underline-offset-2 hover:text-ink-primary hover:underline"
                    >
                      Change…
                    </button>
                  </>
                )}
              </div>
            </div>
          </Card>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sections 4 & 5 — Waiting-on lists (read-only)
// ---------------------------------------------------------------------------

function WaitingList({
  rows,
  emptyMsg,
  tone
}: {
  rows: QueueRow[];
  emptyMsg: string;
  tone: "agent" | "brand";
}) {
  if (rows.length === 0) {
    return (
      <Card>
        <p className="text-sm text-ink-tertiary">{emptyMsg}</p>
      </Card>
    );
  }
  const accent = tone === "agent" ? "text-agent-500" : "text-brand-400";
  return (
    <Card padded={false}>
      <ul className="divide-y divide-subtle/60">
        {rows.slice(0, 12).map((r) => (
          <li
            key={r.id}
            className="flex flex-col gap-1 px-4 py-3 sm:flex-row sm:items-center sm:gap-4 sm:px-5"
          >
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-ink-primary truncate">
                  {r.form_reference || "Untitled queue item"}
                </span>
                <span className="rounded-full bg-white/[0.05] px-1.5 py-0.5 font-mono text-[10px] text-ink-tertiary">
                  TY {r.tax_year}
                </span>
              </div>
              {r.summary ? (
                <p className="mt-0.5 text-[11px] text-ink-tertiary line-clamp-1">
                  {r.summary}
                </p>
              ) : null}
              <p className="mt-0.5 text-[10px] text-ink-tertiary">
                {r.entity_display_name} · {r.submitter_id}
              </p>
            </div>
            <Link
              href={`/taxes/${r.tax_year}?entity_id=${r.entity_id}`}
              className={cn("flex items-center gap-0.5 text-[11px]", accent)}
            >
              View <ChevronRight size={11} />
            </Link>
          </li>
        ))}
        {rows.length > 12 && (
          <li className="px-5 py-2.5 text-center text-[11px] text-ink-tertiary">
            +{rows.length - 12} more — open /taxes/&lt;year&gt; for the full list
          </li>
        )}
      </ul>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Section 6 — Resolved this week
// ---------------------------------------------------------------------------

function ResolvedList({ rows }: { rows: ResolvedRow[] }) {
  if (rows.length === 0) {
    return (
      <Card>
        <p className="text-sm text-ink-tertiary">
          No items resolved in the last 7 days yet — your first win is around
          the corner.
        </p>
      </Card>
    );
  }
  return (
    <Card padded={false}>
      <ul className="divide-y divide-subtle/60">
        {rows.slice(0, 15).map((r) => (
          <li
            key={r.id}
            className="flex flex-col gap-1 px-4 py-2.5 sm:flex-row sm:items-center sm:gap-3 sm:px-5"
          >
            <CheckCircle2
              size={13}
              className="shrink-0 text-credit"
            />
            <div className="min-w-0 flex-1">
              <span className="text-sm text-ink-primary truncate">
                {r.form_reference || "Untitled item"}
              </span>
              <span className="ml-2 rounded-full bg-white/[0.05] px-1.5 py-0.5 font-mono text-[10px] text-ink-tertiary">
                TY {r.tax_year}
              </span>
              {r.entity_display_name ? (
                <span className="ml-1.5 text-[10px] text-ink-tertiary">
                  · {r.entity_display_name}
                </span>
              ) : null}
            </div>
            <span className="text-[10px] text-ink-tertiary">
              {new Date(r.resolved_at).toLocaleDateString()} ·{" "}
              <span className="text-credit">{r.status}</span>
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------

export function OpenItemsView({
  data,
  workspaceId,
  currentTaxYear
}: {
  data: OpenItemsResponse;
  workspaceId: string;
  currentTaxYear: number | null;
}) {
  const allClear =
    data.counts.action_required === 0 &&
    data.counts.waiting_on_mosin === 0 &&
    data.counts.waiting_on_attorney === 0;

  return (
    <div className="space-y-8">
      {/* Top stat strip + filter */}
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-2.5 md:grid-cols-5">
          <CountTile
            label="Action required"
            value={data.counts.action_required}
            tone={data.counts.action_required > 0 ? "debit" : "credit"}
          />
          <CountTile
            label="Time-sensitive"
            value={data.counts.time_sensitive}
            tone={data.counts.time_sensitive > 0 ? "debit" : "ink"}
          />
          <CountTile
            label="Docs to provide"
            value={data.counts.documents_pending}
            tone={data.counts.documents_pending > 0 ? "pending" : "credit"}
          />
          <CountTile
            label="Decisions pending"
            value={data.counts.decisions_pending}
            tone={data.counts.decisions_pending > 0 ? "brand" : "credit"}
          />
          <CountTile
            label="Resolved this week"
            value={data.counts.resolved_this_week}
            tone="credit"
          />
        </div>
        <YearFilter currentTaxYear={currentTaxYear} />
      </div>

      {/* All-clear celebration */}
      {allClear ? (
        <Card className="border-credit/40 bg-credit/[0.04]">
          <div className="flex flex-col items-start gap-3 sm:flex-row sm:items-center">
            <PartyPopper size={28} className="text-credit" />
            <div>
              <h2 className="text-base font-semibold text-credit">
                All clear — nothing on your plate right now.
              </h2>
              <p className="mt-1 text-sm text-ink-secondary">
                No time-sensitive items, no documents pending, no decisions
                queued, and nothing waiting on Mosin or the attorney. Enjoy
                the rare quiet.
              </p>
            </div>
          </div>
        </Card>
      ) : null}

      {/* Section 1 — Time-sensitive */}
      <section id="time-sensitive">
        <SectionHeading
          icon={<AlertTriangle size={15} />}
          title="Time-sensitive"
          count={data.counts.time_sensitive}
          tone={data.counts.time_sensitive > 0 ? "debit" : "credit"}
          subtitle="Blocks downstream work or has an external deadline."
        />
        <TimeSensitiveSection rows={data.time_sensitive} />
      </section>

      {/* Section 2 — Documents */}
      <section id="documents">
        <SectionHeading
          icon={<FileText size={15} />}
          title="Documents to provide"
          count={data.counts.documents_pending}
          tone={data.counts.documents_pending > 0 ? "pending" : "credit"}
          subtitle={`${data.counts.documents_received} received · ${data.counts.documents_pending} still needed`}
        />
        <DocumentsSection
          documents={data.documents}
          workspaceId={workspaceId}
        />
      </section>

      {/* Section 3 — Decisions */}
      <section id="decisions">
        <SectionHeading
          icon={<Scale size={15} />}
          title="Decisions awaiting you"
          count={data.counts.decisions_pending}
          tone={data.counts.decisions_pending > 0 ? "brand" : "credit"}
          subtitle="Each has a recommendation — accept or pick something else."
        />
        <DecisionsSection
          decisions={data.decisions}
          workspaceId={workspaceId}
        />
      </section>

      {/* Section 4 — Waiting on Mosin */}
      <section id="mosin">
        <SectionHeading
          icon={<Hourglass size={15} />}
          title="Waiting on Mosin (CPA)"
          count={data.counts.waiting_on_mosin}
          tone="agent"
          subtitle="Informational — nothing for you to do, but here's what your CPA sees."
        />
        <WaitingList
          rows={data.waiting_on_mosin}
          emptyMsg="Nothing currently waiting on Mosin."
          tone="agent"
        />
      </section>

      {/* Section 5 — Waiting on attorney */}
      <section id="attorney">
        <SectionHeading
          icon={<Gavel size={15} />}
          title="Waiting on attorney"
          count={data.counts.waiting_on_attorney}
          tone="brand"
          subtitle="LLC formation, registered-agent work, Fabrikam structuring."
        />
        <WaitingList
          rows={data.waiting_on_attorney}
          emptyMsg="Nothing currently waiting on the attorney."
          tone="brand"
        />
      </section>

      {/* Section 6 — Resolved this week */}
      <section id="resolved">
        <SectionHeading
          icon={<PartyPopper size={15} />}
          title="Resolved this week"
          count={data.counts.resolved_this_week}
          tone="credit"
          subtitle="Wins from the last 7 days — progress, not just pile-of-work."
        />
        <ResolvedList rows={data.resolved_this_week} />
      </section>

      {/* Footer disclaimer */}
      <p className="text-[11px] text-ink-tertiary">
        as of{" "}
        {new Date(data.as_of).toLocaleString(undefined, {
          dateStyle: "medium",
          timeStyle: "short"
        })}{" "}
        — sourced from <code className="text-ink-secondary">tax_review_queue</code> +
        document checklist. Marking items here writes audit rows; nothing is
        filed.
      </p>
    </div>
  );
}

"use client";

// ClientsTable — CPA command-center view of every workspace the caller has
// membership in. Replaces the prior flat workspaces table with a triage view:
//
//   * Default sort: most-urgent-first (deadline_soon → needs_cpa →
//     waiting_on_client → stale → ok). The backend already returns rows in
//     this order; the UI groups them into labeled sections.
//   * Per row: workspace name + entity count + open-items breakdown +
//     deadline countdown + "Switch to this client" button.
//   * Empty state directs the user to /help on becoming an invited CPA.

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  ArrowRight,
  CalendarClock,
  CheckCircle2,
  ClipboardList,
  HelpCircle,
  ShieldCheck,
  UserCheck,
} from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import type { ClientsTriage, TriageRoute, TriageWorkspace } from "@/lib/api";

const ROUTE_META: Record<
  TriageRoute,
  { label: string; subtitle: string; tone: string; icon: typeof AlertTriangle }
> = {
  deadline_soon: {
    label: "Upcoming filing deadline",
    subtitle: "≤ 14 days to expected acknowledgement",
    tone: "border-debit/40 bg-debit/10 text-debit",
    icon: CalendarClock,
  },
  needs_cpa: {
    label: "Needs CPA action",
    subtitle: "Open tax-review items awaiting your call",
    tone: "border-pending/40 bg-pending/10 text-pending",
    icon: AlertTriangle,
  },
  waiting_on_client: {
    label: "Waiting on client",
    subtitle: "Bank imports pending the workspace owner",
    tone: "border-brand-500/40 bg-brand-500/10 text-brand-300",
    icon: UserCheck,
  },
  stale: {
    label: "Last client activity > 30d",
    subtitle: "Client hasn't fed new data — check in",
    tone: "border-subtle bg-elevated text-ink-tertiary",
    icon: ClipboardList,
  },
  ok: {
    label: "All caught up",
    subtitle: "Nothing waiting on you right now",
    tone: "border-credit/30 bg-credit/[0.05] text-credit",
    icon: CheckCircle2,
  },
};

const ROUTE_ORDER: TriageRoute[] = [
  "deadline_soon",
  "needs_cpa",
  "waiting_on_client",
  "stale",
  "ok",
];

function formatDeadline(days: number | null): string {
  if (days === null) return "No deadline tracked";
  if (days < 0) return `${Math.abs(days)}d overdue`;
  if (days === 0) return "Due today";
  if (days === 1) return "Due tomorrow";
  return `${days}d to go`;
}

function formatLastActivity(iso: string | null): string {
  if (!iso) return "Never";
  const d = new Date(iso);
  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diffSec < 60) return "just now";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  if (diffSec < 86400 * 7) return `${Math.floor(diffSec / 86400)}d ago`;
  return d.toLocaleDateString();
}

export function ClientsTable({
  persona,
  workspaces,
}: {
  persona: ClientsTriage["persona"];
  workspaces: TriageWorkspace[];
}) {
  const router = useRouter();

  function gotoWorkspace(id: string, deepLinkPath = "/dashboard") {
    document.cookie = `aops_workspace=${id}; Path=/; SameSite=Lax; Max-Age=2592000`;
    document.cookie = `aops_entity=; Path=/; SameSite=Lax; Max-Age=0`;
    router.push(deepLinkPath);
  }

  // Group workspaces by suggested_route while preserving the backend order.
  const grouped = new Map<TriageRoute, TriageWorkspace[]>();
  for (const r of ROUTE_ORDER) grouped.set(r, []);
  for (const w of workspaces) {
    const k = w.suggested_route ?? "ok";
    grouped.get(k)?.push(w);
  }

  const empty = workspaces.length === 0;

  return (
    <div className="flex flex-col gap-5">
      <Card padded={false}>
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-subtle px-4 py-3">
          <div>
            <h2 className="text-sm font-semibold text-ink-primary">
              {persona === "cpa"
                ? "Cross-client triage"
                : persona === "platform_admin"
                  ? "All workspaces (platform_admin)"
                  : "Your workspaces"}{" "}
              <span className="text-ink-tertiary">({workspaces.length})</span>
            </h2>
            <p className="mt-0.5 text-[10px] uppercase tracking-wider text-ink-tertiary">
              Most-urgent-first · refreshes per request
            </p>
          </div>
          {persona === "cpa" && (
            <span className="rounded-full bg-pending/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-pending">
              CPA persona
            </span>
          )}
        </div>

        {empty ? (
          <div className="px-4 py-12 text-center text-sm text-ink-tertiary">
            <p className="font-medium text-ink-secondary">
              No clients assigned yet.
            </p>
            <p className="mx-auto mt-1 max-w-md text-xs text-ink-tertiary">
              Workspace owners invite CPAs by email through{" "}
              <code className="text-ink-secondary">/settings/members</code>.
              See{" "}
              <Link
                href="/help"
                className="inline-flex items-center gap-0.5 text-brand-400 hover:underline"
              >
                /help <HelpCircle size={11} />
              </Link>{" "}
              for how to become an invited CPA.
            </p>
          </div>
        ) : (
          <div className="divide-y divide-subtle">
            {ROUTE_ORDER.map((route) => {
              const rows = grouped.get(route) ?? [];
              if (rows.length === 0) return null;
              const meta = ROUTE_META[route];
              const Icon = meta.icon;
              return (
                <section key={route}>
                  <div
                    className={cn(
                      "flex items-center gap-2 border-b border-subtle px-4 py-2",
                      meta.tone
                    )}
                  >
                    <Icon size={13} />
                    <h3 className="text-xs font-semibold uppercase tracking-wider">
                      {meta.label}
                    </h3>
                    <span className="text-[10px] opacity-70">
                      · {meta.subtitle}
                    </span>
                    <span className="ml-auto font-mono text-[10px] opacity-90">
                      {rows.length}
                    </span>
                  </div>
                  <ul className="divide-y divide-subtle">
                    {rows.map((w) => (
                      <li
                        key={w.id}
                        className="flex flex-col gap-3 px-4 py-3 transition hover:bg-white/[0.02] md:flex-row md:items-center md:gap-4"
                      >
                        {/* Identity */}
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="truncate font-medium text-ink-primary">
                              {w.display_name}
                            </span>
                            {w.hipaa_protected ? (
                              <ShieldCheck
                                size={11}
                                className="text-agent-500"
                                aria-label="HIPAA-protected"
                              />
                            ) : null}
                            <span
                              className={cn(
                                "rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider",
                                w.role === "cpa" || w.role === "accountant"
                                  ? "border-pending/40 bg-pending/15 text-pending"
                                  : w.role === "owner" || w.role === "client_owner"
                                    ? "border-brand-500/40 bg-brand-500/15 text-brand-300"
                                    : w.role === "admin"
                                      ? "border-agent-500/30 bg-agent-500/15 text-agent-500"
                                      : "border-subtle bg-white/5 text-ink-tertiary"
                              )}
                            >
                              {w.role}
                            </span>
                          </div>
                          <div className="mt-0.5 text-[11px] text-ink-tertiary">
                            {w.entity_count} entit
                            {w.entity_count === 1 ? "y" : "ies"} ·{" "}
                            <span title="Last client activity (non-agent journal entry)">
                              client active {formatLastActivity(w.last_client_activity_at)}
                            </span>
                          </div>
                        </div>

                        {/* Open-items breakdown */}
                        <div className="grid grid-cols-3 gap-3 text-right md:flex md:gap-4">
                          <Counter
                            label="Needs CPA"
                            n={w.needs_cpa_count}
                            tone={w.needs_cpa_count > 0 ? "pending" : "ink"}
                          />
                          <Counter
                            label="Wait·client"
                            n={w.waiting_on_client_count}
                            tone={w.waiting_on_client_count > 0 ? "brand" : "ink"}
                          />
                          <div className="min-w-[88px]">
                            <p
                              className={cn(
                                "font-mono text-numeric-md tabular-nums",
                                w.days_to_deadline === null
                                  ? "text-ink-tertiary"
                                  : w.days_to_deadline <= 14
                                    ? "text-debit"
                                    : "text-ink-primary"
                              )}
                            >
                              {formatDeadline(w.days_to_deadline)}
                            </p>
                            <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                              deadline
                            </p>
                          </div>
                        </div>

                        {/* CTA */}
                        <div className="flex shrink-0 items-center md:ml-3">
                          <button
                            type="button"
                            onClick={() => gotoWorkspace(w.id)}
                            className="inline-flex items-center gap-1 rounded-md bg-brand-500/15 px-3 py-1.5 text-sm font-medium text-brand-300 transition hover:bg-brand-500/25"
                            aria-label={`Switch to ${w.display_name}`}
                          >
                            Switch <ArrowRight size={12} />
                          </button>
                        </div>
                      </li>
                    ))}
                  </ul>
                </section>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
}

function Counter({
  label,
  n,
  tone,
}: {
  label: string;
  n: number;
  tone: "pending" | "brand" | "ink";
}) {
  return (
    <div className="min-w-[68px]">
      <p
        className={cn(
          "font-mono text-numeric-md tabular-nums",
          tone === "pending" && n > 0 && "text-pending",
          tone === "brand" && n > 0 && "text-brand-300",
          n === 0 && "text-ink-tertiary"
        )}
      >
        {n}
      </p>
      <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
        {label}
      </p>
    </div>
  );
}

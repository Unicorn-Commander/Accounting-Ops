// CFO brief — the "concierge home" card pinned to the TOP of /dashboard.
//
// Plain-language, non-accountant framing of "what needs you now" + cash
// status, with one-tap deep-links. Data comes from the deterministic
// /api/workspaces/{wid}/cfo-brief assembler (see app/api/cfo_brief.py +
// web/lib/cfo-brief.ts). This is a presentational Server Component — it
// renders whatever the brief gives it; the page only renders it when the
// brief loaded (else it degrades to the existing dashboard).

import Link from "next/link";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle,
  Clock,
  FileText,
  MessageSquare,
  ShieldAlert,
  Sparkles
} from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import type {
  CfoBriefPriority,
  CfoBriefResponse
} from "@/lib/cfo-brief";

// Map a priority kind → icon + accent. Kept local so the card is the single
// place that decides the visual language of the brief.
function kindVisuals(p: CfoBriefPriority): {
  Icon: typeof Clock;
  ring: string;
  text: string;
} {
  // Critical severity always reads red, regardless of kind.
  if (p.severity === "critical") {
    return { Icon: ShieldAlert, ring: "bg-debit/15 text-debit", text: "text-debit" };
  }
  switch (p.kind) {
    case "time_sensitive":
    case "filing":
      return {
        Icon: p.kind === "filing" ? Clock : AlertTriangle,
        ring: "bg-debit/15 text-debit",
        text: "text-debit"
      };
    case "decision":
      return {
        Icon: CheckCircle,
        ring: "bg-brand-500/15 text-brand-300",
        text: "text-brand-300"
      };
    case "document":
      return {
        Icon: FileText,
        ring: "bg-pending/15 text-pending",
        text: "text-pending"
      };
    case "finding":
    default:
      return {
        Icon: Sparkles,
        ring: "bg-agent-500/15 text-agent-500",
        text: "text-agent-500"
      };
  }
}

const cashToneText: Record<string, string> = {
  credit: "text-credit",
  debit: "text-debit",
  pending: "text-pending",
  ink: "text-ink-secondary"
};

export function CfoBriefCard({ brief }: { brief: CfoBriefResponse }) {
  // Surface the top 3 in the prominent stack; the rest are summarized as a
  // "+N more" link into the open-items list.
  const headlinePriorities = brief.priorities.slice(0, 3);
  const moreCount = Math.max(
    0,
    brief.action_required_total - headlinePriorities.length
  );
  const allClear = brief.priorities.length === 0;

  return (
    <Card
      padded={false}
      className="mb-6 overflow-hidden border-brand-500/30 bg-gradient-to-br from-brand-500/[0.06] via-surface to-agent-500/[0.04]"
    >
      {/* Header: brand + one-sentence summary + Ask the CFO */}
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-subtle/70 px-5 py-4 md:px-6">
        <div className="flex min-w-0 items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-500/15 text-brand-300 shadow-brand-glow">
            <Sparkles size={18} />
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="text-base font-semibold text-ink-primary">
                Your CFO brief
              </h2>
              <span className="rounded-full bg-white/5 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                Plain English
              </span>
            </div>
            <p className="mt-1 max-w-2xl text-sm text-ink-secondary">
              {brief.summary_line}
            </p>
          </div>
        </div>
        <Link
          href={brief.ask_cfo_href}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-brand-500/40 bg-brand-500/10 px-3 py-1.5 text-xs font-medium text-brand-300 transition hover:border-brand-500/70 hover:text-brand-200"
        >
          <MessageSquare size={13} />
          Ask the CFO
        </Link>
      </div>

      {/* Body */}
      <div className="grid grid-cols-1 gap-0 lg:grid-cols-3">
        {/* Left 2/3: what needs you now */}
        <div className="lg:col-span-2 lg:border-r lg:border-subtle/70">
          <div className="flex items-center justify-between px-5 pb-2 pt-4 md:px-6">
            <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
              What needs you now
            </p>
            {!allClear ? (
              <Link
                href="/open-items"
                className="text-[11px] text-ink-secondary hover:text-ink-primary"
              >
                See everything <ArrowRight size={11} className="inline" />
              </Link>
            ) : null}
          </div>

          {allClear ? (
            <div className="flex items-center gap-3 px-5 py-6 md:px-6">
              <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-credit/15 text-credit">
                <CheckCircle size={16} />
              </span>
              <p className="text-sm text-credit">
                Nothing needs your attention right now. You&apos;re caught up.
              </p>
            </div>
          ) : (
            <ul className="divide-y divide-subtle/50">
              {headlinePriorities.map((p, idx) => {
                const { Icon, ring, text } = kindVisuals(p);
                return (
                  <li key={p.id}>
                    <Link
                      href={p.action_href}
                      className="group flex items-start gap-3 px-5 py-3 transition hover:bg-white/[0.02] md:px-6"
                    >
                      <span
                        className={cn(
                          "mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg",
                          ring
                        )}
                      >
                        <Icon size={14} />
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-[10px] text-ink-tertiary">
                            {idx + 1}
                          </span>
                          <p className="truncate text-sm font-medium text-ink-primary">
                            {p.headline}
                          </p>
                        </div>
                        {p.detail ? (
                          <p className="mt-0.5 line-clamp-2 text-xs text-ink-tertiary">
                            {p.detail}
                          </p>
                        ) : null}
                      </div>
                      <span
                        className={cn(
                          "shrink-0 self-center whitespace-nowrap text-[11px] font-medium opacity-80 group-hover:opacity-100",
                          text
                        )}
                      >
                        {p.action_label}{" "}
                        <ArrowRight size={11} className="inline" />
                      </span>
                    </Link>
                  </li>
                );
              })}
              {moreCount > 0 ? (
                <li>
                  <Link
                    href="/open-items"
                    className="flex items-center gap-2 px-5 py-2.5 text-xs text-ink-secondary hover:text-ink-primary md:px-6"
                  >
                    + {moreCount} more {moreCount === 1 ? "item" : "items"} on
                    your plate
                    <ArrowRight size={11} />
                  </Link>
                </li>
              ) : null}
            </ul>
          )}
        </div>

        {/* Right 1/3: cash status in plain words */}
        <div className="px-5 py-4 md:px-6">
          <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
            Money in the bank
          </p>
          <p
            className={cn(
              "mt-2 font-mono text-numeric-lg tabular-nums",
              cashToneText[brief.cash.tone] ?? "text-ink-primary"
            )}
          >
            {brief.cash.configured
              ? brief.cash.headline
              : "Bank not connected"}
          </p>
          {brief.cash.detail ? (
            <p className="mt-1.5 text-xs text-ink-secondary">
              {brief.cash.detail}
            </p>
          ) : null}
          {brief.cash.configured ? (
            <Link
              href="/transactions"
              className="mt-3 inline-flex items-center gap-1 text-[11px] text-ink-secondary hover:text-ink-primary"
            >
              See money in &amp; out <ArrowRight size={11} />
            </Link>
          ) : (
            <Link
              href="/connectors"
              className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-2.5 py-1.5 text-[11px] font-medium text-white hover:bg-brand-600"
            >
              Connect a bank <ArrowRight size={11} />
            </Link>
          )}
        </div>
      </div>
    </Card>
  );
}

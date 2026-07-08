"use client";

import { useCallback, useRef, useState } from "react";
import Link from "next/link";
import {
  ArrowRight,
  CheckCircle,
  FileText,
  Gavel,
  Plus,
  Shield,
  Sparkles,
} from "lucide-react";
import { CashRibbon } from "@/components/cash-ribbon";
import { AgentStream } from "@/components/agent-stream/agent-stream";
import { LineagePopover, type LineageTarget } from "@/components/lineage-popover";
import { CfoBriefCard } from "@/components/cfo-brief-card";
import type { CashSeries } from "@/lib/cash-series";
import { formatUSD } from "@/lib/cash-position";
import type { CashPosition } from "@/lib/cash-position";
import type { CfoBriefResponse } from "@/lib/cfo-brief";
import type { AiAgreementResponse } from "@/lib/ai-agreement";
import type { AgentDecision } from "@/components/agent-stream/types";

type DashboardV2Props = {
  workspaceId: string;
  workspaceName: string;
  subtitle: string;
  entities: Array<{ id: string; display_name: string }>;
  canConsolidate: boolean;
  entityFilter?: string;
  isConsolidated: boolean;
  cashPosition: CashPosition | null;
  plYtd: { net_income: string; revenue: { total: string }; expenses: { total: string } } | null;
  plMtd: { net_income: string } | null;
  cfoBrief: CfoBriefResponse | null;
  aiAgreement: AiAgreementResponse | null;
  openItemsCount: number;
  initialDecisions: AgentDecision[];
  cashSeries: CashSeries;
  accountCount: number;
  availableNow: number | null;
};

export function DashboardV2({
  workspaceId,
  workspaceName,
  subtitle,
  canConsolidate,
  entityFilter,
  cashPosition,
  plYtd,
  plMtd,
  cfoBrief,
  aiAgreement,
  openItemsCount,
  initialDecisions,
  cashSeries,
  accountCount,
  availableNow,
}: DashboardV2Props) {
  const cashBigRef = useRef<HTMLDivElement | null>(null);
  const anchorRef = useRef<HTMLElement | null>(null);
  const [lineageOpen, setLineageOpen] = useState(false);
  const [lineageTarget, setLineageTarget] = useState<LineageTarget | null>(null);
  const [lineageLabel, setLineageLabel] = useState("");
  const openLineage = useCallback(
    (t: LineageTarget, lbl: string, el: HTMLElement | null) => {
      anchorRef.current = el;
      setLineageTarget(t);
      setLineageLabel(lbl);
      setLineageOpen(true);
    },
    []
  );
  const handleLineageClose = useCallback(() => setLineageOpen(false), []);
  // KPI data
  const netCash = cashPosition ? parseFloat(cashPosition.net_total ?? cashPosition.total_usd ?? "0") : 0;
  const revenueYtd = plYtd ? parseFloat(plYtd.revenue.total) : 0;
  const expensesYtd = plYtd ? parseFloat(plYtd.expenses.total) : 0;
  const netIncomeYtd = plYtd ? parseFloat(plYtd.net_income) : 0;

  // Deltas are honest, neutral labels — NOT fabricated percentages. No
  // sparklines: there is no real per-KPI daily series yet, and a synthetic one
  // would be fake. (Add them back when a real time series ships.)
  const kpis: {
    label: string;
    value: string;
    prefix: string;
    targetValue: number;
    delta: string;
    deltaTone: "credit" | "debit" | "ink";
    target: LineageTarget;
    lineageLabel?: string;
  }[] = [
    {
      label: "Cash in bank",
      value: formatUSD(netCash),
      prefix: "$",
      targetValue: netCash,
      delta: cashPosition?.configured ? "live balance" : "connect a bank",
      deltaTone: "ink",
      target: { metric: "cash" },
      lineageLabel: "ledger cash",
    },
    {
      label: "Revenue YTD",
      value: formatUSD(revenueYtd),
      prefix: "$",
      targetValue: revenueYtd,
      delta: "year to date",
      deltaTone: "ink",
      target: { metric: "revenue", period: "ytd" },
    },
    {
      label: "Expenses YTD",
      value: formatUSD(expensesYtd),
      prefix: "$",
      targetValue: expensesYtd,
      delta: "year to date",
      deltaTone: "ink",
      target: { metric: "expenses", period: "ytd" },
    },
    {
      label: "Net income YTD",
      value: formatUSD(netIncomeYtd),
      prefix: "$",
      targetValue: netIncomeYtd,
      delta: plMtd ? `MTD ${formatUSD(plMtd.net_income)}` : "from journal",
      deltaTone: "ink",
      target: { metric: "net_income", period: "ytd" },
    },
  ];

  return (
    <div className="page">
      {/* Page header */}
      <div className="pagehead">
        <div>
          <h1>Dashboard</h1>
          <div className="sub">{subtitle}</div>
        </div>
        <div className="actions">
          <div className="btn">
            {workspaceName}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="m6 9 6 6 6-6" />
            </svg>
          </div>
          <Link href="/setup?mode=add" className="btn primary">
            <Plus size={15} strokeWidth={2.2} />
            Add entity
          </Link>
        </div>
      </div>

      {/* Hero strip */}
      <div className="hero">
        <div ref={cashBigRef}>
          <CashRibbon
            series={cashSeries.series}
            min={cashSeries.min}
            max={cashSeries.max}
            current={cashSeries.current}
            delta={cashSeries.delta}
            hasHistory={cashSeries.hasHistory}
            accountCount={accountCount}
            availableNow={availableNow}
            annotation={cashSeries.annotation}
            onBigNumClick={() => openLineage({ metric: "cash" }, "ledger cash", cashBigRef.current)}
          />
        </div>

        {/* CFO Brief — REAL data via the shared CfoBriefCard component.
            (Replaces the prototype's hardcoded priorities, which fabricated
            tax advice.) */}
        {cfoBrief ? (
          <CfoBriefCard brief={cfoBrief} />
        ) : (
          <div className="card-depth lift">
            <div className="px-[18px] pt-[18px] pb-3">
              <div className="flex items-center justify-between gap-3 mb-1.5">
                <h3 className="flex items-center gap-2 text-[13px] font-semibold m-0">
                  <Sparkles size={15} className="text-[var(--brand-300)]" />
                  Your CFO brief
                </h3>
              </div>
              <p className="text-[12px] text-[var(--ink-3)] leading-relaxed mt-1">
                Your CFO brief appears here once your books have activity —
                priorities, filings, and decisions the agents surface for you.
              </p>
            </div>
          </div>
        )}
      </div>

      {/* KPI quad */}
      <div className="kpis" id="kpis">
        {kpis.map((kpi, i) => {
          const deltaToneClass =
            kpi.deltaTone === "credit"
              ? "up"
              : kpi.deltaTone === "debit"
                ? "down"
                : "flat";
          return (
            <div
              key={i}
              className="card-depth kpi"
              role="button"
              tabIndex={0}
              style={{ cursor: "pointer" }}
              title={`Trace ${kpi.label} to its journal entries`}
              onClick={(ev) => openLineage(kpi.target, kpi.lineageLabel ?? kpi.value, ev.currentTarget)}
              onKeyDown={(ev) => {
                if (ev.key === "Enter" || ev.key === " ") {
                  ev.preventDefault();
                  openLineage(kpi.target, kpi.lineageLabel ?? kpi.value, ev.currentTarget);
                }
              }}
            >
              <div className="lbl">{kpi.label}</div>
              <div className="v num">
                {kpi.prefix}
                {kpi.targetValue.toLocaleString("en-US", {
                  minimumFractionDigits: 2,
                  maximumFractionDigits: 2,
                })}
              </div>
              <div className="meta">
                <span className={`d ${deltaToneClass}`}>
                  {(kpi.deltaTone === "credit" || kpi.deltaTone === "debit") && (
                    <>{kpi.deltaTone === "debit" ? "▼" : "▲"} </>
                  )}
                  {kpi.delta.replace(/[+\-▲▼]/g, "")}
                </span>
                <span className="trace">trace →</span>
              </div>
            </div>
          );
        })}
      </div>

      {/* Two-column: Agent Stream + Right Rail */}
      <div className="cols">
        {/* Left: Agent stream — real decisions + live polling (demo gated) */}
        <AgentStream
          workspaceId={workspaceId}
          entityId={entityFilter}
          initialDecisions={initialDecisions}
        />

        {/* Right: Account balances + AI Agreement + Compliance */}
        <div className="stack">
          {/* Cash by account */}
          <div className="card-depth lift">
            <div className="px-[18px] pt-[18px] pb-3">
              <div className="flex items-center justify-between gap-3 mb-1.5">
                <h3 className="text-[13px] font-semibold m-0">Cash by account</h3>
                <span className="link">Reconcile →</span>
              </div>
              {cashPosition?.accounts.map((acct) => {
                const bal = parseFloat(acct.balance);
                return (
                  <div key={acct.id} className="acct">
                    <div>
                      <div className="nm">{acct.name}</div>
                      <div className="sb">{acct.org_name}</div>
                    </div>
                    <div
                      className="bal"
                      style={{ color: bal >= 0 ? "var(--credit)" : "var(--debit)" }}
                    >
                      {formatUSD(acct.balance)}
                    </div>
                  </div>
                );
              })}
              {(!cashPosition || cashPosition.accounts.length === 0) && (
                <div className="py-4 text-center text-xs text-[var(--ink-3)]">
                  No accounts connected yet.
                </div>
              )}
            </div>
          </div>

          {/* AI Agreement */}
          <div className="card-depth lift">
            <div className="px-[18px] pt-[18px] pb-3">
              <div className="flex items-center justify-between gap-3 mb-3">
                <h3 className="text-[13px] font-semibold m-0">AI agreement</h3>
                <span className="eyebrow">review vs. redo</span>
              </div>
              <div className="agree">
                <div className="bigring">
                  <b className="num">
                    {aiAgreement
                      ? `${Math.round((aiAgreement.last_90d.agreement_rate ?? 0) * 100)}%`
                      : "—"}
                  </b>
                </div>
                <div>
                  <div style={{ fontSize: "12.5px", fontWeight: 500 }}>
                    You agreed with every decision
                  </div>
                  <div className="muted" style={{ fontSize: "11.5px", marginTop: 4 }}>
                    {aiAgreement
                      ? `${aiAgreement.last_90d.agreed} of ${aiAgreement.last_90d.total_decided} reviewed this month.`
                      : "No decisions reviewed yet."}
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Compliance watch */}
          <div className="card-depth lift">
            <div className="px-[18px] pt-[18px] pb-3">
              <div className="flex items-center justify-between gap-3 mb-2">
                <h3 className="text-[13px] font-semibold m-0">Compliance watch</h3>
                <span className="link">Add rule →</span>
              </div>
              <div style={{ display: "flex", gap: 10, alignItems: "flex-start", padding: "8px 0" }}>
                <div
                  className="cfo"
                  style={{
                    width: 28,
                    height: 28,
                    borderRadius: 8,
                    display: "grid",
                    placeItems: "center",
                    background: "hsla(265 80% 65% / 0.15)",
                    color: "var(--brand-300)",
                    flex: "none",
                  }}
                >
                  <Shield size={14} />
                </div>
                <div style={{ fontSize: 12, color: "var(--ink-2)", lineHeight: 1.5 }}>
                  No active rules yet. Add a 1099-NEC trigger, sales-tax nexus
                  tracker, or FERS earned-income watch on{" "}
                  <Link href="/specialty-rules" className="text-[var(--brand-300)] underline-offset-2 hover:underline">
                    Specialty rules
                  </Link>{" "}
                  to populate this panel.
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Toast region */}
      <div className="toasts" id="toasts" />

      {/* Lineage popover — anchored to cash big number; real provenance +
          live chain verification (no fabricated hash). */}
      <LineagePopover
        open={lineageOpen}
        onClose={handleLineageClose}
        anchorRef={anchorRef}
        workspaceId={workspaceId}
        entityId={entityFilter}
        target={lineageTarget}
        label={lineageLabel}
      />
    </div>
  );
}

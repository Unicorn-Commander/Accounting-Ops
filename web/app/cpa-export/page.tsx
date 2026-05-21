import { Download, FileSpreadsheet, FileText, Lock } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { Button } from "@/components/ui/button";

const FORMATS = [
  {
    name: "QuickBooks Online (IIF)",
    description: "Importable into QBO via Banking -> File Upload. Includes chart of accounts mapping + reasoning traces as notes.",
    icon: <FileSpreadsheet size={18} />,
    primary: true
  },
  {
    name: "Xero (CSV)",
    description: "Xero-compatible transaction CSV with our reasoning column preserved. Drop into Xero Bank Statements importer.",
    icon: <FileSpreadsheet size={18} />,
    primary: false
  },
  {
    name: "Plain-text journal",
    description: "Beancount/hledger format. Append-only, source-readable. The most defensible export at audit time.",
    icon: <FileText size={18} />,
    primary: false
  },
  {
    name: "Working papers (PDF)",
    description: "Full audit bundle: trial balance, reasoning traces per entry, source-document hashes, decision log, exception history.",
    icon: <FileText size={18} />,
    primary: false
  }
];

const PERIODS = [
  { label: "May 2026 (in progress)", status: "open" },
  { label: "April 2026", status: "closed" },
  { label: "March 2026", status: "closed" },
  { label: "Q1 2026", status: "closed" },
  { label: "FY 2025", status: "filed" }
];

export default function CpaExportPage() {
  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="CPA handoff"
          subtitle="Export bundles for your accountant. Period-locked. Reasoning trails preserved. Multiple formats so any CPA can import."
        />

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <Card padded={false} className="lg:col-span-1">
            <div className="border-b border-subtle px-5 py-3">
              <h2 className="text-sm font-semibold">Period</h2>
            </div>
            <ul className="divide-y divide-subtle/60">
              {PERIODS.map((p) => (
                <li
                  key={p.label}
                  className="flex items-center justify-between px-5 py-3 transition hover:bg-white/[0.02]"
                >
                  <div className="flex items-center gap-2">
                    {p.status === "filed" && <Lock size={11} className="text-ink-tertiary" />}
                    <span className="text-sm">{p.label}</span>
                  </div>
                  {p.status === "open" && <StatusChip tone="pending">Open</StatusChip>}
                  {p.status === "closed" && <StatusChip tone="ink">Closed</StatusChip>}
                  {p.status === "filed" && <StatusChip tone="credit">Filed</StatusChip>}
                </li>
              ))}
            </ul>
          </Card>

          <div className="space-y-3 lg:col-span-2">
            {FORMATS.map((f) => (
              <Card key={f.name}>
                <div className="flex items-start gap-4">
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-elevated text-brand-400 ring-1 ring-inset ring-brand-500/30">
                    {f.icon}
                  </div>
                  <div className="flex-1">
                    <div className="flex items-start justify-between gap-3">
                      <h3 className="text-base font-semibold">{f.name}</h3>
                      <Button variant={f.primary ? "primary" : "secondary"} size="sm">
                        <Download size={13} /> Export
                      </Button>
                    </div>
                    <p className="mt-1 text-sm leading-relaxed text-ink-secondary">{f.description}</p>
                  </div>
                </div>
              </Card>
            ))}
          </div>
        </div>

        <Card className="mt-8 border-pending/30 bg-pending/[0.04]">
          <h3 className="text-sm font-semibold">Accountant access (read-only) — Phase 3</h3>
          <p className="mt-2 text-sm leading-relaxed text-ink-secondary">
            Invite your CPA to a time-locked read-only view of any closed period. They see the trial balance + reasoning + working papers + ask the agent questions, but cannot modify your books. Closes the loop without giving them write access.
          </p>
        </Card>
      </PageShell>
    </AppShell>
  );
}

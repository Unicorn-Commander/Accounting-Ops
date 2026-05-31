import { Pause, Play, Plus, Settings } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { Button } from "@/components/ui/button";

const RULES = [
  { kind: "1099_nec_trigger", name: "1099-NEC threshold", description: "Cumulative payments to any contractor crossing $600 in the calendar year trigger 1099-NEC reporting.", trigger: "threshold", action: "notify", source: "user_setup", active: true, hits: 2 },
  { kind: "sales_tax_nexus_state", name: "Multi-state sales tax nexus", description: "Per-state nexus thresholds. Watches trailing 12mo revenue and transaction-count per state. Currently TX, CA, NY, FL armed.", trigger: "threshold", action: "require_approval", source: "user_setup", active: true, hits: 1 },
  { kind: "material_amount_approval", name: "Materiality gate", description: "Auto-post below $100. Queue review $100-$1,000. Block over $1,000 until human-attested.", trigger: "threshold", action: "require_approval", source: "user_setup", active: true, hits: 14 },
  { kind: "entity_distribution_isolation", name: "C-Corp distribution isolation", description: "Distributions from Magic Unicorn Inc to Aaron personal entity never auto-post as personal earned income. Always queues for explicit categorization.", trigger: "event", action: "block", source: "user_setup", active: true, hits: 0 },
  { kind: "income_threshold_warning", name: "Personal earned-income ceiling", description: "Aaron personal entity. Warning at 70%, hard-flag at 80% of configured threshold for the calendar year.", trigger: "schedule", action: "notify", source: "ai_inferred", active: true, hits: 0 },
  { kind: "foreign_payment_flag", name: "Foreign payment review", description: "Any payment to a non-US recipient gets tagged for FBAR/FATCA review at year-end. Wise USDC payouts to international contributors flow through here.", trigger: "event", action: "tag", source: "user_setup", active: true, hits: 8 },
  { kind: "qsbs_holding_period", name: "QSBS §1202 holding period", description: "Tracks the 5-year holding period for QSBS-eligible shares per stockholder. Inactive until first issuance.", trigger: "schedule", action: "notify", source: "user_setup", active: false, hits: 0 },
  { kind: "sdvosb_ownership_threshold", name: "SDVOSB 51% ownership", description: "Watches Magic Unicorn cap table for the 51% service-disabled-veteran ownership floor required for SDVOSB certification.", trigger: "event", action: "notify", source: "user_setup", active: false, hits: 0 }
];

export default function SpecialtyRulesPage() {
  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Specialty rules"
          subtitle="Per-tenant policy engine. Vertical compliance lives here. Each rule has temporal validity and an auditable change history."
          action={
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center gap-1 rounded-full border border-pending/40 bg-pending/[0.08] px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-pending">
                Preview
              </span>
              <Button variant="primary" size="sm">
                <Plus size={13} /> Add rule
              </Button>
            </div>
          }
        />

        {/* Preview banner — audit quick-win #6 + issue 16: rule editing is
            inert sample data, not a wired policy engine. */}
        <div className="mb-4 rounded-lg border border-pending/40 bg-pending/[0.04] px-4 py-3 text-xs text-pending">
          <strong className="font-semibold">Preview:</strong> rules below are
          a static reference of what specialty rules will look like. Editing,
          activation, and trigger counts are not wired to the policy engine
          yet.
        </div>

        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {RULES.map((r) => (
            <Card key={r.kind} className={r.active ? "" : "opacity-60"}>
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2">
                    <h3 className="text-base font-semibold">{r.name}</h3>
                    {r.active ? (
                      <StatusChip tone="credit">Active</StatusChip>
                    ) : (
                      <StatusChip tone="ink">Inactive</StatusChip>
                    )}
                  </div>
                  <p className="mt-0.5 font-mono text-[11px] text-ink-tertiary">{r.kind}</p>
                </div>
                <button
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
                  title={r.active ? "Pause rule" : "Activate rule"}
                >
                  {r.active ? <Pause size={13} /> : <Play size={13} />}
                </button>
              </div>
              <p className="mt-3 text-sm leading-relaxed text-ink-secondary">{r.description}</p>
              <dl className="mt-4 grid grid-cols-3 gap-3 text-[11px]">
                <div>
                  <dt className="uppercase tracking-wider text-ink-tertiary">Trigger</dt>
                  <dd className="mt-0.5 font-mono">{r.trigger}</dd>
                </div>
                <div>
                  <dt className="uppercase tracking-wider text-ink-tertiary">Action</dt>
                  <dd className="mt-0.5 font-mono">{r.action}</dd>
                </div>
                <div>
                  <dt className="uppercase tracking-wider text-ink-tertiary">Hits / YTD</dt>
                  <dd className="mt-0.5 font-mono">{r.hits}</dd>
                </div>
              </dl>
              <div className="mt-4 flex items-center justify-between text-[11px] text-ink-tertiary">
                <span>Source: {r.source.replace("_", " ")}</span>
                <button className="inline-flex items-center gap-1 hover:text-ink-primary">
                  <Settings size={11} /> Configure
                </button>
              </div>
            </Card>
          ))}
        </div>

        <p className="mt-6 text-xs text-ink-tertiary">
          Want a rule we don&apos;t ship? Describe it in chat — the Compliance agent can draft a new rule for your review.
        </p>
      </PageShell>
    </AppShell>
  );
}

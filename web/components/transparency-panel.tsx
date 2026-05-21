"use client";

import * as Collapsible from "@radix-ui/react-collapsible";
import { ChevronDown, Code, FileText, GitBranch, MessageSquare, Wrench } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";

const DEMO_DECISION = {
  tool: "categorize_transaction",
  reasoning:
    "Counterparty 'Anthropic, PBC' matched prior software-expense pattern with confidence 0.97. Materiality $200 is below tenant's review threshold ($1,000). Vendor name found in 4 prior journal entries categorized as 6210·SaaS·AI.",
  rule: "smart_categorization@2026-05-21#7a3c",
  source: "sha256:5f3a8b21c9...e1d4 · receipts/inv-abc-123.pdf",
  model: "qwen-3.6-35b-a3b-q4",
  confidence: 0.97,
  args: {
    tenant_id: "8b3c4d5e-1234-5678-9abc-def012345678",
    transaction_id: "tx_01J1K2L3M4N5",
    category: "6210·SaaS·AI",
    reasoning: "Matched prior pattern, materiality low, auto-post permitted."
  }
};

export function TransparencyPanel() {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible.Root open={open} onOpenChange={setOpen}>
      <Collapsible.Trigger className="flex w-full items-center justify-between px-6 py-4 text-left transition hover:bg-white/[0.02]">
        <div className="flex items-center gap-3">
          <GitBranch size={16} className="text-agent-500" />
          <div>
            <p className="text-sm font-semibold">Show me why</p>
            <p className="text-xs text-ink-tertiary">
              Tool call · reasoning · rule · source document · model version
            </p>
          </div>
        </div>
        <ChevronDown
          size={16}
          className={cn(
            "text-ink-tertiary transition-transform duration-200",
            open && "rotate-180"
          )}
        />
      </Collapsible.Trigger>
      <Collapsible.Content className="data-[state=open]:animate-fade-in">
        <div className="grid grid-cols-1 gap-3 border-t border-subtle bg-canvas/40 p-6 md:grid-cols-2">
          <Panel icon={<Wrench size={14} />} title="MCP tool call">
            <pre className="overflow-auto rounded-md bg-canvas p-3 font-mono text-[11px] leading-relaxed">
              {JSON.stringify({ tool: DEMO_DECISION.tool, args: DEMO_DECISION.args }, null, 2)}
            </pre>
          </Panel>
          <Panel icon={<MessageSquare size={14} />} title="Reasoning trace">
            <p className="font-reasoning text-sm leading-relaxed text-ink-secondary">
              {DEMO_DECISION.reasoning}
            </p>
          </Panel>
          <Panel icon={<Code size={14} />} title="Rule + model">
            <dl className="space-y-2 text-sm">
              <Row label="Rule version" value={DEMO_DECISION.rule} mono />
              <Row label="Model" value={DEMO_DECISION.model} mono />
              <Row
                label="Confidence"
                value={
                  <span className="inline-flex items-center gap-2">
                    <span
                      className={cn(
                        "inline-block h-1.5 w-1.5 rounded-full",
                        DEMO_DECISION.confidence >= 0.9
                          ? "bg-conf-high"
                          : DEMO_DECISION.confidence >= 0.7
                            ? "bg-conf-medium"
                            : "bg-conf-low"
                      )}
                    />
                    {(DEMO_DECISION.confidence * 100).toFixed(0)}% · Confident
                  </span>
                }
              />
            </dl>
          </Panel>
          <Panel icon={<FileText size={14} />} title="Source document">
            <div className="rounded-md border border-subtle bg-canvas/60 p-3">
              <p className="font-mono text-[11px] text-ink-tertiary">{DEMO_DECISION.source}</p>
              <p className="mt-2 text-xs text-ink-secondary">
                Content-hash addressed in Garage. Tamper-evidence via Merkle chain anchored to bigboy.
              </p>
            </div>
          </Panel>
        </div>
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

function Panel({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-subtle bg-surface p-4">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-tertiary">
        <span className="text-agent-500">{icon}</span>
        <span>{title}</span>
      </div>
      {children}
    </div>
  );
}

function Row({
  label,
  value,
  mono
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="shrink-0 text-ink-tertiary">{label}</dt>
      <dd className={cn("text-right text-ink-primary", mono && "font-mono text-xs")}>{value}</dd>
    </div>
  );
}

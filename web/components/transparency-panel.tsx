"use client";

import * as Collapsible from "@radix-ui/react-collapsible";
import { ChevronDown, FileText, GitBranch, Wrench } from "lucide-react";
import { useState } from "react";

const trace = {
  tool: "categorize_transaction",
  reasoning: "Known counterparty matched prior software expense pattern; materiality below tenant review threshold.",
  rule: "material_amount_approval@2026-05-21",
  source: "sha256:placeholder-receipt-thumbnail",
  model: "qwen-3.6-35b-a3b-q4"
};

export function TransparencyPanel() {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible.Root open={open} onOpenChange={setOpen} className="border-t border-line bg-paper">
      <Collapsible.Trigger className="flex w-full items-center justify-between px-4 py-3 text-left text-sm font-semibold">
        <span className="flex items-center gap-2">
          <GitBranch size={16} /> Decision trace
        </span>
        <ChevronDown size={16} className={open ? "rotate-180 transition" : "transition"} />
      </Collapsible.Trigger>
      <Collapsible.Content>
        <div className="grid gap-3 px-4 pb-4 md:grid-cols-2">
          <div className="rounded-md border border-line p-3">
            <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
              <Wrench size={15} /> MCP tool call
            </div>
            <pre className="overflow-auto text-xs">{JSON.stringify({ tool: trace.tool, args: { tenant_id: "placeholder" } }, null, 2)}</pre>
          </div>
          <div className="rounded-md border border-line p-3">
            <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
              <FileText size={15} /> Source and rule
            </div>
            <dl className="space-y-2 text-sm">
              <div><dt className="font-semibold">Reasoning</dt><dd>{trace.reasoning}</dd></div>
              <div><dt className="font-semibold">Rule</dt><dd>{trace.rule}</dd></div>
              <div><dt className="font-semibold">Source</dt><dd>{trace.source}</dd></div>
              <div><dt className="font-semibold">Model</dt><dd>{trace.model}</dd></div>
            </dl>
          </div>
        </div>
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

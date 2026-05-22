"use client";

import { useState } from "react";
import { ChevronDown, X } from "lucide-react";

// Phase 2: this becomes a real SSE connection to
//   GET /api/v1/activity/stream?since=<Last-Event-ID>&filters=...
// rendering live agent decisions from the orchestrator. Until then we just
// show an honest empty state so the UI doesn't lie.

export function ActivityTicker({ defaultOpen = true }: { defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-4 right-4 z-40 flex items-center gap-2 rounded-full border border-default bg-elevated/95 px-3 py-2 text-xs text-ink-secondary shadow-ticker backdrop-blur hover:bg-elevated"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-ink-disabled" />
        Agent activity
        <ChevronDown size={14} className="rotate-180" />
      </button>
    );
  }

  return (
    <aside className="fixed bottom-4 right-4 z-40 w-80 max-w-[calc(100vw-2rem)] overflow-hidden rounded-lg border border-default bg-elevated/95 shadow-ticker backdrop-blur">
      <header className="flex items-center gap-2 border-b border-subtle px-3 py-2">
        <span className="h-1.5 w-1.5 rounded-full bg-ink-disabled" />
        <p className="text-xs font-semibold">Agent activity</p>
        <span className="ml-auto font-mono text-[10px] text-ink-tertiary">Phase 2</span>
        <button
          onClick={() => setOpen(false)}
          className="ml-1 rounded p-0.5 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
          aria-label="Collapse activity ticker"
        >
          <X size={13} />
        </button>
      </header>
      <div className="px-3 py-6 text-center text-[11px] text-ink-tertiary">
        <p>No agent activity yet.</p>
        <p className="mt-1">
          This widget streams live decisions from Brigade once Phase 2 wires the
          orchestrator + receipt-VLM pipeline.
        </p>
      </div>
    </aside>
  );
}

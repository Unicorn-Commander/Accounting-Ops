"use client";

import { useEffect, useRef, useState } from "react";
import { Shield, Bot, Undo2, FileCheck2 } from "lucide-react";
import { formatUSD } from "@/lib/cash-position";

// What number is being traced. Mirrors the backend /lineage `metric` param.
export type LineageTarget = {
  metric: "cash" | "revenue" | "expenses" | "net_income" | "account";
  account?: string;
  period?: string;
  asOf?: string;
};

type AgentDecision = {
  tool_name?: string | null;
  model_version?: string | null;
  reasoning_trace?: string | null;
  outcome?: string | null;
};

type LineageEntry = {
  id: string;
  effective_date: string | null;
  amount: string;
  debit_account: string;
  credit_account: string;
  category: string | null;
  materiality_tier: string | null;
  status: string;
  source: string | null;
  payee: string | null;
  description: string | null;
  reasoning_trace: string | null;
  model_version: string | null;
  decision_verdict: string | null;
  agent_decision: AgentDecision | null;
  source_document_hash: string | null;
  reverses_entry_id: string | null;
  chain_ok: boolean | null;
};

type LineageResponse = {
  account_label: string;
  period_label: string;
  summary: {
    net: string;
    entry_count: number;
    returned: number;
    chain: { available: boolean; ok?: boolean; tampered_count?: number };
  };
  entries: LineageEntry[];
};

type State =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; data: LineageResponse }
  | { kind: "error" };

function acctShort(canonical: string): string {
  // "4010·Revenue · Marketplace" → "4010 Marketplace" (compact for the row)
  const [num, ...rest] = canonical.split("·");
  const name = rest.join("·").trim();
  return `${num.trim()}${name ? " " + name : ""}`;
}

export function LineagePopover({
  open,
  onClose,
  anchorRef,
  workspaceId,
  entityId,
  target,
  label,
}: {
  open: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLElement | null>;
  workspaceId: string;
  entityId?: string;
  target: LineageTarget | null;
  label: string;
}) {
  const popRef = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<State>({ kind: "idle" });

  // Position next to the anchor.
  useEffect(() => {
    if (!open || !anchorRef.current || !popRef.current) return;
    const r = anchorRef.current.getBoundingClientRect();
    popRef.current.style.left = `${Math.min(r.left, window.innerWidth - 400)}px`;
    popRef.current.style.top = `${r.bottom + 10}px`;
  }, [open, anchorRef]);

  // Fetch the real lineage (entries + provenance + chain) on open.
  useEffect(() => {
    if (!open || !target) return;
    let cancelled = false;
    setState({ kind: "loading" });
    const params = new URLSearchParams({ metric: target.metric });
    if (target.account) params.set("account", target.account);
    if (target.period) params.set("period", target.period);
    if (target.asOf) params.set("as_of", target.asOf);
    if (entityId) params.set("entity_id", entityId);
    fetch(`/api/workspaces/${workspaceId}/lineage?${params.toString()}`, { cache: "no-store" })
      .then((res) => (res.ok ? (res.json() as Promise<LineageResponse>) : Promise.reject()))
      .then((data) => {
        if (!cancelled) setState({ kind: "ready", data });
      })
      .catch(() => {
        if (!cancelled) setState({ kind: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [open, target, workspaceId, entityId]);

  // Click-outside + Escape.
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (popRef.current && !popRef.current.contains(e.target as Node)) onClose();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);

  if (!open) return null;

  const data = state.kind === "ready" ? state.data : null;
  const chain = data?.summary.chain;
  const chainBad = chain?.available && chain.ok === false;

  return (
    <div className="lin" ref={popRef} style={{ display: "block" }}>
      <div className="h">
        <b>Where {label} comes from</b>
        <span className="x" onClick={onClose}>
          ✕
        </span>
      </div>

      {/* Summary: how many real entries roll up to this number + the net. */}
      <div className="linsum">
        {state.kind === "loading" && <span>Tracing the journal…</span>}
        {state.kind === "error" && <span>Lineage unavailable.</span>}
        {data && (
          <>
            <span className="big">{data.summary.entry_count}</span>
            <span className="lbl">
              {data.summary.entry_count === 1 ? "entry" : "entries"} · net {formatUSD(data.summary.net)}
            </span>
            <span className="spacer" />
            <span className="scope">{data.period_label}</span>
          </>
        )}
      </div>

      {/* The contributing entries — full provenance + agent trail + reversal. */}
      {data && data.entries.length > 0 && (
        <div className="linlist" aria-label="contributing journal entries">
          {data.entries.map((e) => {
            const agentModel = e.agent_decision?.model_version ?? e.model_version;
            const why = e.agent_decision?.reasoning_trace ?? e.reasoning_trace ?? undefined;
            return (
              <div className="linrow" key={e.id} title={why}>
                <div className="top">
                  <span className="dt">{e.effective_date ?? "—"}</span>
                  <span className="amt">{formatUSD(e.amount)}</span>
                </div>
                <div className="flow">
                  {acctShort(e.debit_account)} <span className="arr">→</span> {acctShort(e.credit_account)}
                </div>
                {(e.payee || e.description || e.category) && (
                  <div className="desc">{e.payee || e.description || e.category}</div>
                )}
                <div className="tags">
                  {e.source && <span className="tag">{e.source}</span>}
                  {agentModel && (
                    <span className="tag agent" title={why}>
                      <Bot size={10} /> {agentModel}
                    </span>
                  )}
                  {e.reverses_entry_id && (
                    <span className="tag rev">
                      <Undo2 size={10} /> reversal
                    </span>
                  )}
                  {e.source_document_hash && (
                    <span className="tag" title={`source doc ${e.source_document_hash.slice(0, 12)}…`}>
                      <FileCheck2 size={10} /> receipt
                    </span>
                  )}
                  {e.chain_ok === false && <span className="tag bad">⚠ tampered</span>}
                  {e.materiality_tier && <span className={`tag m${e.materiality_tier}`}>{e.materiality_tier}</span>}
                </div>
              </div>
            );
          })}
          {data.summary.entry_count > data.summary.returned && (
            <div className="linmore">
              +{data.summary.entry_count - data.summary.returned} more · showing the most recent {data.summary.returned}
            </div>
          )}
        </div>
      )}

      {data && data.entries.length === 0 && state.kind === "ready" && (
        <div className="linempty">No posted journal entries roll up to this number yet.</div>
      )}

      {/* Tamper-evidence footer (hash chain). */}
      <div
        className="hashbar"
        style={chainBad ? { background: "hsla(0 91% 71% / 0.08)", borderColor: "hsla(0 91% 71% / 0.2)", color: "var(--debit)" } : undefined}
      >
        <Shield size={13} />
        {!chain || !chain.available
          ? "Tamper-evident · hash-chained journal"
          : chain.ok
            ? "Verified · hash chain intact"
            : `Tamper detected · ${chain.tampered_count} affected`}
      </div>
    </div>
  );
}

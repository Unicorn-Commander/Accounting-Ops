"use client";

import { useState, useTransition } from "react";
import { Loader2, Shield, ShieldCheck, ShieldX } from "lucide-react";

type ChainResult = {
  ok: boolean;
  tampered_count: number;
  tampered: Array<{ entry_id: string; position: number; stored_hash_hex: string; computed_hash_hex: string }>;
};

export function VerifyChainButton({
  workspaceId,
  entityId
}: {
  workspaceId: string;
  entityId: string;
}) {
  const [pending, startTransition] = useTransition();
  const [result, setResult] = useState<ChainResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  function verify() {
    setError(null);
    startTransition(async () => {
      try {
        const res = await fetch(
          `/api/workspaces/${workspaceId}/journal-entries/verify-chain?entity_id=${entityId}`
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = (await res.json()) as ChainResult;
        setResult(body);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
      }
    });
  }

  if (result) {
    return (
      <span
        className={
          "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium " +
          (result.ok
            ? "bg-credit/10 text-credit"
            : "bg-debit/15 text-debit")
        }
        title={
          result.ok
            ? "Chain integrity verified"
            : `${result.tampered_count} tampered entries detected`
        }
      >
        {result.ok ? <ShieldCheck size={10} /> : <ShieldX size={10} />}
        {result.ok ? "chain ok" : `tampered ${result.tampered_count}`}
      </span>
    );
  }

  return (
    <button
      onClick={verify}
      disabled={pending}
      className="inline-flex items-center gap-1 rounded-full bg-ink-tertiary/10 px-2 py-0.5 text-[10px] text-ink-tertiary hover:bg-ink-tertiary/20"
      title={error ?? "Re-verify the per-entity hash chain"}
    >
      {pending ? <Loader2 size={10} className="animate-spin" /> : <Shield size={10} />}
      verify chain
    </button>
  );
}

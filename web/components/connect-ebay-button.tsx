"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ExternalLink, Loader2, Plus, ShoppingBag, Unlink } from "lucide-react";

import { cn } from "@/lib/cn";

export function ConnectEbayButton({
  workspaceId,
  entityId,
  configured,
  sellerHandle
}: {
  workspaceId: string;
  entityId: string | null;
  configured: boolean;
  sellerHandle: string | null;
}) {
  const router = useRouter();
  const [disconnecting, setDisconnecting] = useState(false);

  if (!entityId) {
    return (
      <button
        disabled
        title="Pick a workspace + entity first (top-right)"
        className="inline-flex items-center gap-1 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white opacity-60"
      >
        <ShoppingBag size={12} />
        Connect eBay (pick entity)
      </button>
    );
  }

  if (!configured) {
    const href = `/api/connectors/ebay/start?workspace_id=${encodeURIComponent(
      workspaceId
    )}&entity_id=${encodeURIComponent(entityId)}`;
    return (
      <a
        href={href}
        className="inline-flex items-center gap-1 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600 transition"
      >
        <Plus size={12} />
        Connect eBay
        <ExternalLink size={11} className="ml-0.5 opacity-80" />
      </a>
    );
  }

  async function disconnect() {
    if (
      !confirm(
        `Disconnect eBay (${sellerHandle ?? "current seller"})? Existing journal entries are not deleted; the credential is wiped so the sync worker stops pulling.`
      )
    ) {
      return;
    }
    setDisconnecting(true);
    try {
      const res = await fetch(
        `/api/proxy/api/workspaces/${workspaceId}/connectors/ebay`,
        { method: "DELETE" }
      );
      if (res.ok || res.status === 204) {
        router.refresh();
      } else {
        const body = await res.text();
        alert(`Disconnect failed: ${body || res.status}`);
      }
    } catch (err) {
      alert(`Disconnect failed: ${err instanceof Error ? err.message : "unknown"}`);
    } finally {
      setDisconnecting(false);
    }
  }

  return (
    <button
      onClick={disconnect}
      disabled={disconnecting}
      className={cn(
        "inline-flex items-center gap-1 rounded-md border border-default bg-surface px-3 py-1.5 text-xs text-ink-secondary hover:bg-elevated hover:text-debit transition disabled:cursor-not-allowed disabled:opacity-60"
      )}
    >
      {disconnecting ? (
        <Loader2 size={12} className="animate-spin" />
      ) : (
        <Unlink size={12} />
      )}
      {disconnecting ? "Disconnecting…" : "Disconnect"}
    </button>
  );
}

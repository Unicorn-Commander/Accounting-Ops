"use client";

import { useEffect, useRef, useState } from "react";
import { Shield } from "lucide-react";

type ChainResult = {
  ok: boolean;
  tampered_count: number;
};

type VerifyState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; ok: boolean; tampered: number }
  | { kind: "error" }
  | { kind: "na" };

export function LineagePopover({
  open,
  onClose,
  anchorRef,
  workspaceId,
  entityId,
  cashLabel,
  scopeLabel,
  accountName,
  latestDecision,
}: {
  open: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLElement | null>;
  workspaceId: string;
  entityId?: string;
  cashLabel: string;
  scopeLabel: string;
  accountName: string | null;
  latestDecision: { label: string; title: string; status: string } | null;
}) {
  const popRef = useRef<HTMLDivElement>(null);
  const [verify, setVerify] = useState<VerifyState>({ kind: "idle" });

  // Position next to the anchor.
  useEffect(() => {
    if (!open || !anchorRef.current || !popRef.current) return;
    const r = anchorRef.current.getBoundingClientRect();
    popRef.current.style.left = `${Math.min(r.left, window.innerWidth - 360)}px`;
    popRef.current.style.top = `${r.bottom + 10}px`;
  }, [open, anchorRef]);

  // Real chain verification on open. Per-entity only (consolidated has no single chain).
  useEffect(() => {
    if (!open) return;
    if (!entityId) {
      setVerify({ kind: "na" });
      return;
    }
    let cancelled = false;
    setVerify({ kind: "loading" });
    fetch(
      `/api/workspaces/${workspaceId}/journal-entries/verify-chain?entity_id=${encodeURIComponent(entityId)}`,
      { cache: "no-store" }
    )
      .then((res) => (res.ok ? (res.json() as Promise<ChainResult>) : Promise.reject()))
      .then((body) => {
        if (!cancelled) setVerify({ kind: "ok", ok: body.ok, tampered: body.tampered_count });
      })
      .catch(() => {
        if (!cancelled) setVerify({ kind: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [open, workspaceId, entityId]);

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

  const journalLine =
    verify.kind === "loading"
      ? "Verifying hash chain…"
      : verify.kind === "ok"
        ? verify.ok
          ? "Hash chain intact"
          : `${verify.tampered} tampered ${verify.tampered === 1 ? "entry" : "entries"}`
        : verify.kind === "na"
          ? "Hash-chained · pick an entity to verify"
          : "Chain status unavailable";

  const decisionLine = latestDecision
    ? `${latestDecision.label} · ${latestDecision.title}`
    : "No recent agent decision";

  return (
    <div className="lin" ref={popRef} style={{ display: "block" }}>
      <div className="h">
        <b>Where {cashLabel} comes from</b>
        <span className="x" onClick={onClose}>
          ✕
        </span>
      </div>
      <div className="chain">
        <div className="step" style={{ "--c2": "157 64% 52%" } as React.CSSProperties}>
          <div className="n">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="13" height="13">
              <path d="M3 3v18h18" />
              <path d="m7 12 3 3 7-7" />
            </svg>
          </div>
          <div>
            <div className="l">Report line</div>
            <div className="v">
              Net cash position <small>· {scopeLabel}</small>
            </div>
          </div>
        </div>
        <div className="step" style={{ "--c2": "280 75% 68%" } as React.CSSProperties}>
          <div className="n">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="13" height="13">
              <path d="M4 4h16v16H4z" />
              <path d="M4 9h16M9 4v16" />
            </svg>
          </div>
          <div>
            <div className="l">Journal</div>
            <div className="v">{journalLine}</div>
          </div>
        </div>
        <div className="step" style={{ "--c2": "220 90% 66%" } as React.CSSProperties}>
          <div className="n">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="13" height="13">
              <rect x="3" y="5" width="18" height="14" rx="2" />
              <path d="M3 10h18" />
            </svg>
          </div>
          <div>
            <div className="l">Source</div>
            <div className="v">{accountName ? `${accountName}` : "Connected accounts"}</div>
          </div>
        </div>
        <div className="step" style={{ "--c2": "188 95% 57%" } as React.CSSProperties}>
          <div className="n">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="13" height="13">
              <circle cx="12" cy="12" r="3" />
              <path d="M12 2v3M12 19v3M2 12h3M19 12h3" />
            </svg>
          </div>
          <div>
            <div className="l">Latest agent decision</div>
            <div className="v" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 280 }}>
              {decisionLine}
            </div>
          </div>
        </div>
      </div>
      <div
        className="hashbar"
        style={
          verify.kind === "ok" && !verify.ok
            ? { background: "hsla(0 91% 71% / 0.08)", borderColor: "hsla(0 91% 71% / 0.2)", color: "var(--debit)" }
            : undefined
        }
      >
        <Shield size={13} />
        {verify.kind === "loading"
          ? "Verifying…"
          : verify.kind === "ok"
            ? verify.ok
              ? "Verified · hash chain intact"
              : `Tamper detected · ${verify.tampered} affected`
            : verify.kind === "na"
              ? "Tamper-evident · hash-chained journal"
              : "Verification unavailable"}
      </div>
    </div>
  );
}

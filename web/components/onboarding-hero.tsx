"use client";

// OnboardingHero — the PRIMARY first-run path: "drag in your documents +
// connect your bank, we figure it out". The Setup Assistant (rendered below
// this hero on /setup) is the guided/handheld ALTERNATIVE.
//
// Two first-class actions, side by side:
//   1. Drop documents  → DocumentUploader (compact) bound to the entity.
//   2. Connect your bank → the existing ConnectSimplefinModal (SimpleFIN).
//
// A collapsible "Prefer to be guided?" note points at the Setup Assistant chat
// that lives beneath. This component never blocks: if no entity is bound yet
// (first-run before the placeholder bootstrap completes), the upload card shows
// a gentle "pick/await an entity" state instead of erroring.

import { useState } from "react";
import { Banknote, ChevronDown, FileUp, MessageSquare, Sparkles } from "lucide-react";
import { ConnectSimplefinModal } from "@/components/connect-simplefin-modal";
import { DocumentUploader } from "@/components/document-uploader";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";

export function OnboardingHero({
  workspaceId,
  workspaceName,
  entityId,
  entityName,
}: {
  workspaceId: string;
  workspaceName: string;
  entityId: string | null;
  entityName?: string;
}) {
  const [showGuided, setShowGuided] = useState(false);

  return (
    <div className="mb-5 space-y-3">
      <Card>
        <div className="flex items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-500/15 text-brand-300">
            <Sparkles size={18} />
          </span>
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-ink-primary">
              The fastest way to start: just dump everything
            </h2>
            <p className="mt-1 text-sm text-ink-secondary">
              Drag in your receipts, invoices, and statements, and connect your
              bank. We read what we can, propose entries, and queue everything for
              your review. Nothing posts to your books without you.
            </p>
          </div>
        </div>

        <div className="mt-5 grid grid-cols-1 gap-4 md:grid-cols-2">
          {/* Action 1 — drop documents */}
          <div className="rounded-lg border border-default bg-canvas/40 p-4">
            <div className="mb-3 flex items-center gap-2">
              <FileUp size={15} className="text-brand-400" />
              <h3 className="text-sm font-semibold text-ink-primary">
                Drop your documents
              </h3>
            </div>
            <DocumentUploader
              workspaceId={workspaceId}
              entityId={entityId}
              entityName={entityName}
              compact
            />
          </div>

          {/* Action 2 — connect bank */}
          <div className="flex flex-col rounded-lg border border-default bg-canvas/40 p-4">
            <div className="mb-3 flex items-center gap-2">
              <Banknote size={15} className="text-brand-400" />
              <h3 className="text-sm font-semibold text-ink-primary">
                Connect your bank
              </h3>
            </div>
            <p className="flex-1 text-xs text-ink-tertiary">
              Link your bank + cards via SimpleFIN. We pull balances and
              transactions read-only; the first batch goes to your approval queue,
              never straight to the ledger.
            </p>
            <div className="mt-4">
              <ConnectSimplefinModal
                workspaceId={workspaceId}
                workspaceName={workspaceName}
                trigger={
                  <button className="inline-flex w-full items-center justify-center gap-1.5 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-600">
                    <Banknote size={14} />
                    Connect a bank
                  </button>
                }
              />
            </div>
          </div>
        </div>

        {/* Guided alternative */}
        <button
          type="button"
          onClick={() => setShowGuided((s) => !s)}
          className="mt-4 inline-flex items-center gap-1.5 text-xs text-ink-tertiary hover:text-ink-primary"
        >
          <MessageSquare size={12} />
          Prefer to be guided step by step?
          <ChevronDown
            size={12}
            className={cn("transition", showGuided && "rotate-180")}
          />
        </button>
        {showGuided && (
          <p className="mt-2 rounded-md border border-subtle bg-elevated/50 px-3 py-2 text-xs text-ink-secondary">
            No problem — the <strong>Setup Assistant</strong> below will walk you
            through your business profile, entity, tax facts, banking, and chart
            of accounts one question at a time. You can do both: dump documents
            now and chat with the assistant whenever you like.
          </p>
        )}
      </Card>
    </div>
  );
}

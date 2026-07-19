// /documents — the PRIMARY "dump everything, we figure it out" surface.
//
// Server Component:
//  - Resolves the active workspace + entity from cookies.
//  - Renders the DocumentUploader (drag-drop multi-file) bound to that entity.
//  - Lists already-uploaded documents (metadata only) with their review status.
//
// Uploaded documents are stored in Garage, content-hash-deduped, run through the
// LOCAL receipt VLM (images only), and queued in the approval queue for human
// review. NOTHING is auto-posted to the ledger.

import Link from "next/link";
import { ArrowRight, FileText, Image as ImageIcon, Inbox, Receipt } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { DocumentUploader } from "@/components/document-uploader";
import { EntitySelector } from "@/components/entity-selector";
import { Card, EmptyState, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { getDocuments, type UploadedDocument } from "@/lib/documents";

export const dynamic = "force-dynamic";

export default async function DocumentsPage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const entity = ctx.isConsolidated ? null : ctx.activeEntity;
  const canConsolidate = workspace
    ? ["owner", "admin"].includes(workspace.role)
    : false;

  if (!workspace) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader
            title="Documents"
            subtitle="Drag in receipts, invoices, and statements — we read them and queue them for review."
          />
          <EmptyState
            variant="permission-denied"
            title="No workspace yet"
            description="You need a workspace before you can upload documents. Ask an admin to invite you."
          />
        </PageShell>
      </AppShell>
    );
  }

  const docList = entity
    ? await getDocuments(workspace.id, entity.id, { limit: 50 })
    : null;
  const documents = docList?.documents ?? [];

  // Brand-new workspace with no entities yet: there's nothing to file documents
  // under. Send the user through the Setup Assistant to create their first
  // entity instead of stranding them on a disabled drop zone.
  if (ctx.entities.length === 0) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader
            title="Documents"
            subtitle="Drag in receipts, invoices, and statements — we read them and queue them for review."
          />
          <EmptyState
            variant="not-connected"
            title="Let's set up your business first"
            description="Documents are filed under a specific business's books, so you'll create your first one in a quick guided setup. After that, drop in receipts here (the on-prem AI reads them — nothing leaves the server) or connect your bank to pull transactions automatically."
            primaryAction={
              <Link
                href="/setup"
                className="inline-flex items-center gap-1 rounded-lg bg-brand-500/15 px-3 py-1.5 text-xs font-medium text-brand-400 transition hover:bg-brand-500/25"
              >
                Start setup <ArrowRight size={12} />
              </Link>
            }
          />
        </PageShell>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Documents"
          subtitle="Drag in everything — receipts, invoices, PDFs, bank CSVs. We read receipts automatically and queue every document for your review. Nothing posts to your books without you."
          action={
            <EntitySelector
              entities={ctx.entities}
              activeEntityId={ctx.isConsolidated ? "consolidated" : entity?.id}
              canConsolidate={canConsolidate}
            />
          }
        />

        {ctx.isConsolidated ? (
          <Card>
            <p className="text-sm text-ink-secondary">
              Pick a single entity above to upload documents — each document is
              filed under one entity&apos;s books.
            </p>
          </Card>
        ) : (
          <DocumentUploader
            workspaceId={workspace.id}
            entityId={entity?.id ?? null}
            entityName={entity?.display_name}
          />
        )}

        {/* Uploaded documents list */}
        {entity && (
          <div id="uploaded-documents" className="mt-8 scroll-mt-6">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold uppercase tracking-wider text-ink-tertiary">
                Uploaded documents
              </h2>
              <Link
                href="/approvals"
                className="inline-flex items-center gap-1 text-xs text-ink-secondary hover:text-ink-primary"
              >
                Review queue <ArrowRight size={12} />
              </Link>
            </div>

            {documents.length === 0 ? (
              <EmptyState
                variant="no-data"
                icon={<Inbox size={28} className="text-ink-disabled" />}
                title="No documents uploaded yet"
                description="Drag a receipt, invoice, or statement into the box above — the on-prem AI reads the vendor, date, and amount (nothing leaves the server), then queues it here for your approval. Prefer the fast path? Connecting your bank pulls transactions in automatically."
                primaryAction={
                  <Link
                    href="/connectors"
                    className="inline-flex items-center gap-1 rounded-lg bg-brand-500/15 px-3 py-1.5 text-xs font-medium text-brand-400 transition hover:bg-brand-500/25"
                  >
                    Connect your bank <ArrowRight size={12} />
                  </Link>
                }
              />
            ) : (
              <Card padded={false}>
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[640px] text-sm">
                    <thead className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                      <tr className="border-b border-subtle">
                        <th className="px-5 py-2.5 text-left font-medium">Document</th>
                        <th className="px-2 py-2.5 text-left font-medium">Type</th>
                        <th className="px-2 py-2.5 text-left font-medium">Amount</th>
                        <th className="px-2 py-2.5 text-left font-medium">Status</th>
                        <th className="px-5 py-2.5 text-right font-medium">Uploaded</th>
                      </tr>
                    </thead>
                    <tbody>
                      {documents.map((d) => (
                        <DocRow key={d.id} doc={d} />
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            )}
          </div>
        )}
      </PageShell>
    </AppShell>
  );
}

function DocRow({ doc }: { doc: UploadedDocument }) {
  // Null-guard every field: a list row must never throw (and take down the
  // whole documents segment) on a transient/incomplete record.
  const mime = doc.mime_type ?? "";
  const isImage = mime.startsWith("image/");
  const isPdf = mime === "application/pdf";
  const Icon = isImage ? ImageIcon : isPdf ? FileText : Receipt;
  const kindLabel = isImage ? "Receipt" : isPdf ? "PDF" : mime.includes("csv") ? "CSV" : "Doc";
  // A recognized tax form (1099-R, W-2, SSA-1099, …) gets its form type + year
  // surfaced; 'other'/null falls back to the generic kind. 'failed' = the OCR
  // ran but couldn't read it.
  const isTaxDoc = !!doc.doc_type && doc.doc_type !== "other";
  const extractionFailed = doc.extraction_status === "failed";

  return (
    <tr className="border-b border-subtle/60 last:border-b-0 hover:bg-white/[0.02]">
      <td className="px-5 py-3">
        <div className="flex items-center gap-2.5">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-elevated text-ink-tertiary">
            <Icon size={13} />
          </span>
          <span className="font-mono text-[11px] text-ink-tertiary" title={doc.content_hash ?? ""}>
            {(doc.content_hash ?? "").slice(0, 12)}…
          </span>
        </div>
      </td>
      <td className="px-2 py-3 text-xs">
        {isTaxDoc ? (
          <span className="font-medium text-brand-400">
            {doc.doc_type}
            {doc.tax_year ? (
              <span className="text-ink-tertiary"> · {doc.tax_year}</span>
            ) : null}
          </span>
        ) : extractionFailed ? (
          <span className="text-ink-tertiary" title="Couldn't read this document automatically">
            {kindLabel} <span className="text-pending">· unread</span>
          </span>
        ) : (
          <span className="text-ink-secondary">{kindLabel}</span>
        )}
      </td>
      <td className="px-2 py-3 font-mono text-xs tabular-nums text-ink-secondary">
        {doc.amount ? `${doc.currency_code ?? ""} ${doc.amount}`.trim() : "—"}
      </td>
      <td className="px-2 py-3">
        <ReviewStatusChip status={doc.review_status} />
      </td>
      <td className="px-5 py-3 text-right font-mono text-[11px] text-ink-tertiary">
        {doc.created_at
          ? new Date(doc.created_at).toLocaleDateString("en-US", {
              month: "short",
              day: "numeric",
            })
          : "—"}
      </td>
    </tr>
  );
}

function ReviewStatusChip({ status }: { status: string | null }) {
  if (status === "approved") return <StatusChip tone="credit">Approved</StatusChip>;
  if (status === "rejected") return <StatusChip tone="debit">Rejected</StatusChip>;
  if (status === "skipped") return <StatusChip tone="ink">Skipped</StatusChip>;
  if (status === "pending") return <StatusChip tone="pending">Awaiting review</StatusChip>;
  return <StatusChip tone="ink">—</StatusChip>;
}

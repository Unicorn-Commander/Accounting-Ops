"use client";

// DocumentUploader — the PRIMARY "drag in everything, we figure it out" surface.
//
// Multi-file drag-drop + click-to-browse. Each file is uploaded one at a time to
//   POST /api/workspaces/{wid}/entities/{eid}/documents/upload
// (a dedicated multipart proxy → app/api/documents.py). The backend stores the
// file in Garage, content-hash-dedups it, runs the LOCAL receipt VLM on images,
// and queues a 'document_review' approval item. NOTHING is auto-posted.
//
// Degrades gracefully: a single file failing never blocks the others; each row
// shows its own status (queued / duplicate / read / needs-review / error). The
// component never throws into the page.

import { useCallback, useId, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  CheckCircle2,
  CloudUpload,
  Copy,
  FileText,
  Image as ImageIcon,
  Loader2,
  Receipt,
  Sparkles,
  X,
} from "lucide-react";
import { cn } from "@/lib/cn";

// Mirror of the backend allow-list (for the file <input accept> + a friendly
// client-side pre-check; the server is the source of truth).
const ACCEPT =
  ".pdf,.jpg,.jpeg,.png,.heic,.heif,.webp,.tiff,.tif,.gif,.csv,application/pdf,image/*,text/csv";

type UploadStatus = "pending" | "uploading" | "done" | "duplicate" | "error";

type UploadResult = {
  ok?: boolean;
  duplicate?: boolean;
  receipt_id?: string;
  extraction_tier?: string | null;
  extracted?: {
    vendor: string | null;
    amount: string | null;
    date: string | null;
    currency: string | null;
    tier: string;
  } | null;
  approval_id?: string | null;
  message?: string;
  detail?: string;
};

type Row = {
  key: string;
  name: string;
  size: number;
  mime: string;
  status: UploadStatus;
  result?: UploadResult;
  error?: string;
};

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function iconFor(mime: string) {
  if (mime.startsWith("image/")) return ImageIcon;
  if (mime === "application/pdf") return FileText;
  return Receipt;
}

export function DocumentUploader({
  workspaceId,
  entityId,
  entityName,
  compact = false,
  onUploaded,
}: {
  workspaceId: string;
  entityId: string | null;
  entityName?: string;
  /** Compact = a single drop card with no result log (for onboarding). */
  compact?: boolean;
  /** Fired after each successful (non-duplicate) upload — e.g. to refresh. */
  onUploaded?: (result: UploadResult) => void;
}) {
  const router = useRouter();
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);

  const disabled = !entityId;

  const uploadOne = useCallback(
    async (file: File): Promise<{ status: UploadStatus; result?: UploadResult; error?: string }> => {
      if (!entityId) {
        return { status: "error", error: "No entity selected." };
      }
      const form = new FormData();
      form.append("file", file, file.name);
      try {
        const res = await fetch(
          `/api/workspaces/${workspaceId}/entities/${entityId}/documents/upload`,
          { method: "POST", body: form }
        );
        const data = (await res.json().catch(() => ({}))) as UploadResult;
        if (!res.ok) {
          return {
            status: "error",
            error: data.detail || `Upload failed (HTTP ${res.status})`,
          };
        }
        return {
          status: data.duplicate ? "duplicate" : "done",
          result: data,
        };
      } catch (e) {
        return {
          status: "error",
          error: e instanceof Error ? e.message : "Network error",
        };
      }
    },
    [workspaceId, entityId]
  );

  const handleFiles = useCallback(
    async (fileList: FileList | File[]) => {
      const files = Array.from(fileList);
      if (files.length === 0) return;

      // Seed rows as pending.
      const seeded: Row[] = files.map((f, i) => ({
        key: `${Date.now()}-${i}-${f.name}`,
        name: f.name,
        size: f.size,
        mime: f.type || "application/octet-stream",
        status: "pending" as UploadStatus,
      }));
      setRows((prev) => [...seeded, ...prev]);
      setBusy(true);

      // Upload sequentially so the local VLM isn't hammered with N concurrent
      // image jobs (it's a single shared GPU box) and so the queue order is
      // predictable.
      for (let i = 0; i < files.length; i++) {
        const row = seeded[i];
        setRows((prev) =>
          prev.map((r) => (r.key === row.key ? { ...r, status: "uploading" } : r))
        );
        const outcome = await uploadOne(files[i]);
        setRows((prev) =>
          prev.map((r) =>
            r.key === row.key
              ? { ...r, status: outcome.status, result: outcome.result, error: outcome.error }
              : r
          )
        );
        if (outcome.status === "done" && outcome.result) {
          onUploaded?.(outcome.result);
        }
      }

      setBusy(false);
      // Refresh server components (e.g. the documents list + approvals badge).
      router.refresh();
    },
    [uploadOne, onUploaded, router]
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      if (disabled) return;
      if (e.dataTransfer?.files?.length) {
        void handleFiles(e.dataTransfer.files);
      }
    },
    [handleFiles, disabled]
  );

  const completedCount = rows.filter(
    (r) => r.status === "done" || r.status === "duplicate"
  ).length;

  return (
    <div className="space-y-4">
      {/* Drop zone */}
      <div
        role="button"
        tabIndex={0}
        aria-disabled={disabled}
        onClick={() => !disabled && inputRef.current?.click()}
        onKeyDown={(e) => {
          if (!disabled && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={cn(
          "group relative flex cursor-pointer flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed px-6 text-center transition",
          compact ? "py-8" : "py-12",
          dragging
            ? "border-brand-500 bg-brand-500/[0.06]"
            : "border-strong bg-surface/40 hover:border-brand-500/60 hover:bg-surface",
          disabled && "cursor-not-allowed opacity-60"
        )}
      >
        <input
          ref={inputRef}
          id={inputId}
          type="file"
          multiple
          accept={ACCEPT}
          className="sr-only"
          disabled={disabled}
          onChange={(e) => {
            if (e.target.files?.length) void handleFiles(e.target.files);
            // Reset so re-selecting the same file fires onChange again.
            e.target.value = "";
          }}
        />
        <span
          className={cn(
            "flex h-12 w-12 items-center justify-center rounded-full transition",
            dragging ? "bg-brand-500/20 text-brand-300" : "bg-elevated text-brand-400"
          )}
        >
          <CloudUpload size={22} />
        </span>
        <div>
          <p className="text-sm font-semibold text-ink-primary">
            {dragging ? "Drop to upload" : "Drag in your documents — or click to browse"}
          </p>
          <p className="mt-1 text-xs text-ink-tertiary">
            Receipts, invoices, PDFs, bank CSVs. We read receipts automatically and
            queue everything for your review.{" "}
            {entityName ? (
              <span className="text-ink-secondary">Filing under {entityName}.</span>
            ) : null}
          </p>
        </div>
        {!compact && (
          <p className="text-[10px] uppercase tracking-wider text-ink-disabled">
            PDF · JPG · PNG · HEIC · WEBP · TIFF · CSV · up to 25 MB each
          </p>
        )}
        {busy && (
          <span className="pointer-events-none absolute right-3 top-3 inline-flex items-center gap-1 text-[11px] text-brand-300">
            <Loader2 size={12} className="animate-spin" /> Uploading…
          </span>
        )}
      </div>

      {disabled && (
        <p className="rounded-md border border-pending/40 bg-pending/[0.06] px-3 py-2 text-xs text-pending">
          Pick an entity first — uploaded documents are filed under a specific
          entity&apos;s books.
        </p>
      )}

      {/* Result log (hidden in compact mode) */}
      {!compact && rows.length > 0 && (
        <div className="rounded-lg border border-default bg-surface">
          <div className="flex items-center justify-between border-b border-subtle px-4 py-2.5">
            <span className="text-xs font-medium text-ink-secondary">
              {completedCount} of {rows.length} processed
            </span>
            <button
              type="button"
              onClick={() => setRows([])}
              disabled={busy}
              className="text-[11px] text-ink-tertiary hover:text-ink-primary disabled:opacity-50"
            >
              Clear
            </button>
          </div>
          <ul className="divide-y divide-subtle/60">
            {rows.map((row) => (
              <UploadRow key={row.key} row={row} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function UploadRow({ row }: { row: Row }) {
  const Icon = iconFor(row.mime);
  const ex = row.result?.extracted;
  return (
    <li className="flex items-start gap-3 px-4 py-3">
      <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-elevated text-ink-tertiary">
        <Icon size={15} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm text-ink-primary" title={row.name}>
            {row.name}
          </span>
          <span className="shrink-0 font-mono text-[10px] text-ink-tertiary">
            {humanSize(row.size)}
          </span>
        </div>

        {/* Extraction preview for read receipts */}
        {ex && (ex.vendor || ex.amount || ex.date) && (
          <p className="mt-0.5 text-[11px] text-ink-secondary">
            {[ex.vendor, ex.amount ? `${ex.currency ?? ""} ${ex.amount}`.trim() : null, ex.date]
              .filter(Boolean)
              .join(" · ")}
          </p>
        )}
        {row.error && (
          <p className="mt-0.5 text-[11px] text-debit">{row.error}</p>
        )}
        {row.result?.message && !row.error && (
          <p className="mt-0.5 text-[11px] text-ink-tertiary">{row.result.message}</p>
        )}
      </div>
      <StatusPill row={row} />
    </li>
  );
}

function StatusPill({ row }: { row: Row }) {
  if (row.status === "uploading" || row.status === "pending") {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 text-[11px] text-brand-300">
        <Loader2 size={12} className="animate-spin" />
        {row.status === "pending" ? "Queued" : "Uploading"}
      </span>
    );
  }
  if (row.status === "error") {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 text-[11px] text-debit">
        <AlertTriangle size={12} /> Failed
      </span>
    );
  }
  if (row.status === "duplicate") {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 text-[11px] text-ink-tertiary">
        <Copy size={12} /> Already uploaded
      </span>
    );
  }
  // done
  const tier = row.result?.extraction_tier;
  if (tier === "high") {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 text-[11px] text-credit">
        <Sparkles size={12} /> Read · queued
      </span>
    );
  }
  if (tier === "low") {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 text-[11px] text-pending">
        <AlertTriangle size={12} /> Needs review
      </span>
    );
  }
  return (
    <span className="inline-flex shrink-0 items-center gap-1 text-[11px] text-credit">
      <CheckCircle2 size={12} /> Queued
    </span>
  );
}

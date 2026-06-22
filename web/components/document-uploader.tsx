"use client";

// DocumentUploader — the PRIMARY "drag in everything, we figure it out" surface.
//
// Multi-file AND whole-FOLDER drag-drop, plus click-to-browse (files) and a
// "choose a folder" picker. Each file is uploaded one at a time to
//   POST /api/workspaces/{wid}/entities/{eid}/documents/upload
// (a dedicated multipart proxy → app/api/documents.py). The backend stores the
// file in Garage, content-hash-dedups it, runs the LOCAL receipt VLM on images,
// and queues a 'document_review' approval item. NOTHING is auto-posted.
//
// Dropping/picking a FOLDER walks it recursively (webkitGetAsEntry); junk and
// oversized files are filtered client-side and shown as "skipped" so the run is
// honest. Degrades gracefully: a single file failing never blocks the others;
// each row shows its own status (queued / duplicate / read / needs-review /
// skipped / error). The component never throws into the page.

import { useCallback, useId, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  CloudUpload,
  Copy,
  FileText,
  FolderUp,
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

// Keep in sync with the backend default DOCUMENT_UPLOAD_MAX_BYTES + the edge
// proxy route. Oversized files are skipped client-side with a clear reason
// rather than firing a doomed request the server would 413.
const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

// Extension allow-list (mirrors ACCEPT) — used to filter junk when a whole
// FOLDER is dropped or picked (folders routinely carry .DS_Store, .docx, etc.).
const ACCEPT_EXTS = new Set([
  "pdf", "jpg", "jpeg", "png", "heic", "heif", "webp", "tiff", "tif", "gif", "csv",
]);
function isAcceptedName(name: string): boolean {
  const dot = name.lastIndexOf(".");
  return dot >= 0 && ACCEPT_EXTS.has(name.slice(dot + 1).toLowerCase());
}

// Monotonic row-key source. Date.now() can repeat within a millisecond across
// rapid/successive folder batches and collide React keys + the r.key===row.key
// matching in the upload loop — a counter is collision-free.
let rowSeq = 0;
const nextRowKey = (name: string): string => `row-${rowSeq++}-${name}`;

// --- folder-aware drop handling -------------------------------------------
// dataTransfer.files OMITS the contents of dropped folders. The FileSystem
// entries API (webkitGetAsEntry) exposes them, but the DataTransferItemList is
// invalidated once the drop handler returns — so grab the entries SYNCHRONOUSLY
// (entriesFromDrop) and walk them async, recursively (walkEntry).
function entriesFromDrop(dt: DataTransfer): FileSystemEntry[] {
  const out: FileSystemEntry[] = [];
  const items = dt.items;
  if (!items) return out;
  for (let i = 0; i < items.length; i++) {
    const entry = items[i].webkitGetAsEntry?.();
    if (entry) out.push(entry);
  }
  return out;
}

async function walkEntry(entry: FileSystemEntry, out: File[]): Promise<void> {
  if (entry.isFile) {
    const file = await new Promise<File | null>((resolve) =>
      (entry as FileSystemFileEntry).file(
        (f) => resolve(f),
        () => resolve(null)
      )
    );
    if (file) out.push(file);
  } else if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    // readEntries yields at most ~100 entries per call — loop until empty.
    for (;;) {
      const batch = await new Promise<FileSystemEntry[]>((resolve) =>
        reader.readEntries(
          (e) => resolve(e),
          () => resolve([])
        )
      );
      if (batch.length === 0) break;
      for (const child of batch) await walkEntry(child, out);
    }
  }
}

type UploadStatus =
  | "pending"
  | "uploading"
  | "done"
  | "duplicate"
  | "error"
  | "skipped";

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
  const folderInputRef = useRef<HTMLInputElement | null>(null);
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
      // Drop dotfiles (.DS_Store, ._resource forks) that ride along in folders.
      const incoming = Array.from(fileList).filter((f) => !f.name.startsWith("."));
      if (incoming.length === 0) return;

      // A dropped/picked FOLDER brings everything inside it — partition into
      // uploadable files vs. skips (unsupported type or over the size cap) so we
      // don't fire doomed requests for the junk a folder inevitably contains.
      const maxMb = Math.round(MAX_UPLOAD_BYTES / (1024 * 1024));
      const accepted: File[] = [];
      const skippedRows: Row[] = [];
      for (const f of incoming) {
        if (!isAcceptedName(f.name)) {
          skippedRows.push({
            key: nextRowKey(f.name),
            name: f.name,
            size: f.size,
            mime: f.type || "",
            status: "skipped",
            error: "Unsupported type",
          });
        } else if (f.size > MAX_UPLOAD_BYTES) {
          skippedRows.push({
            key: nextRowKey(f.name),
            name: f.name,
            size: f.size,
            mime: f.type || "",
            status: "skipped",
            error: `Too large (max ${maxMb} MB)`,
          });
        } else {
          accepted.push(f);
        }
      }

      // Seed accepted rows as pending (+ any skipped rows, for transparency).
      const seeded: Row[] = accepted.map((f) => ({
        key: nextRowKey(f.name),
        name: f.name,
        size: f.size,
        mime: f.type || "application/octet-stream",
        status: "pending" as UploadStatus,
      }));
      setRows((prev) => [...seeded, ...skippedRows, ...prev]);
      if (accepted.length === 0) return; // nothing uploadable — only skips
      setBusy(true);

      // Upload sequentially so the local VLM isn't hammered with N concurrent
      // image jobs (it's a single shared GPU box) and so the queue order is
      // predictable.
      const outcomes: UploadStatus[] = [];
      for (let i = 0; i < accepted.length; i++) {
        const row = seeded[i];
        setRows((prev) =>
          prev.map((r) => (r.key === row.key ? { ...r, status: "uploading" } : r))
        );
        const outcome = await uploadOne(accepted[i]);
        outcomes.push(outcome.status);
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
      // Scroll the freshly-updated "Uploaded documents" list into view so a
      // first-time user can see their file actually landed in the system.
      if (!compact && outcomes.some((s) => s === "done")) {
        requestAnimationFrame(() => {
          document
            .getElementById("uploaded-documents")
            ?.scrollIntoView({ behavior: "smooth", block: "start" });
        });
      }
    },
    [uploadOne, onUploaded, router, compact]
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      if (disabled) return;
      const dt = e.dataTransfer;
      if (!dt) return;
      // Capture folder entries SYNCHRONOUSLY (the item list dies after this
      // handler returns), then walk them — including nested folders — before
      // handing the flattened file list to the uploader.
      const entries = entriesFromDrop(dt);
      if (entries.length > 0) {
        const collected: File[] = [];
        void (async () => {
          for (const entry of entries) await walkEntry(entry, collected);
          if (collected.length > 0) await handleFiles(collected);
        })();
      } else if (dt.files?.length) {
        void handleFiles(dt.files);
      }
    },
    [handleFiles, disabled]
  );

  const completedCount = rows.filter(
    (r) => r.status === "done" || r.status === "duplicate"
  ).length;

  // Once a run finishes, surface a clear "it landed" confirmation that names the
  // files and reassures a brand-new user the documents are now queued for review.
  const doneRows = rows.filter((r) => r.status === "done");
  const dupeRows = rows.filter((r) => r.status === "duplicate");
  const errorRows = rows.filter((r) => r.status === "error");
  const skipRows = rows.filter((r) => r.status === "skipped");
  const showSuccess = !busy && !compact && doneRows.length > 0;
  const successNames = doneRows.map((r) => r.name);
  const successLabel =
    successNames.length === 1
      ? successNames[0]
      : successNames.length === 2
        ? `${successNames[0]} and ${successNames[1]}`
        : `${successNames[0]} and ${successNames.length - 1} more`;

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
        {/* Folder picker — a single <input> can't be both multiple-files and a
            directory, so the "whole folder" path gets its own input. The
            webkitdirectory/directory attributes aren't in React's input typings,
            so they're set imperatively via the ref callback. */}
        <input
          ref={(el) => {
            folderInputRef.current = el;
            if (el) {
              el.setAttribute("webkitdirectory", "");
              el.setAttribute("directory", "");
            }
          }}
          type="file"
          multiple
          className="sr-only"
          disabled={disabled}
          onChange={(e) => {
            if (e.target.files?.length) void handleFiles(e.target.files);
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
            {dragging
              ? "Drop to upload"
              : "Drag in documents or a whole folder — or click to browse"}
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
            PDF · JPG · PNG · HEIC · WEBP · TIFF · CSV · up to 100 MB each
          </p>
        )}
        {busy && (
          <span className="pointer-events-none absolute right-3 top-3 inline-flex items-center gap-1 text-[11px] text-brand-300">
            <Loader2 size={12} className="animate-spin" /> Uploading…
          </span>
        )}
      </div>

      {/* Whole-folder affordance — kept OUTSIDE the drop zone so it isn't an
          interactive control nested inside the role="button" card. Rendered even
          when disabled (greyed) so the capability stays discoverable. */}
      <div className="-mt-1 text-center">
        <button
          type="button"
          disabled={disabled}
          onClick={() => !disabled && folderInputRef.current?.click()}
          className="inline-flex items-center gap-1.5 text-xs font-medium text-brand-400 underline-offset-2 hover:underline disabled:cursor-not-allowed disabled:no-underline disabled:opacity-50"
        >
          <FolderUp size={13} /> Upload a whole folder
        </button>
      </div>

      {disabled && (
        <p className="rounded-md border border-pending/40 bg-pending/[0.06] px-3 py-2 text-xs text-pending">
          Pick an entity first — uploaded documents are filed under a specific
          entity&apos;s books.
        </p>
      )}

      {/* Success confirmation — makes it obvious the files landed and are queued. */}
      {showSuccess && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-credit/40 bg-credit/[0.07] px-4 py-3">
          <div className="flex items-start gap-2.5">
            <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-credit" />
            <p className="text-sm text-credit">
              <span className="font-semibold">
                {doneRows.length === 1
                  ? "1 document added"
                  : `${doneRows.length} documents added`}
              </span>{" "}
              <span className="text-credit/90">
                — {successLabel} {doneRows.length === 1 ? "is" : "are"} queued for
                review.
              </span>
              {dupeRows.length > 0 && (
                <span className="text-credit/70">
                  {" "}
                  {dupeRows.length} already uploaded earlier.
                </span>
              )}
              {skipRows.length > 0 && (
                <span className="text-ink-tertiary">
                  {" "}
                  {skipRows.length} skipped (unsupported or too large).
                </span>
              )}
              {errorRows.length > 0 && (
                <span className="text-pending">
                  {" "}
                  {errorRows.length} couldn&apos;t be read — see below.
                </span>
              )}
            </p>
          </div>
          <Link
            href="/approvals"
            className="inline-flex shrink-0 items-center gap-1 rounded-md bg-credit/10 px-2.5 py-1 text-xs font-medium text-credit hover:bg-credit/20"
          >
            Review now <ArrowRight size={12} />
          </Link>
        </div>
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
  const isSkip = row.status === "skipped";
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
          <p className={cn("mt-0.5 text-[11px]", isSkip ? "text-ink-tertiary" : "text-debit")}>
            {row.error}
          </p>
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
  if (row.status === "skipped") {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 text-[11px] text-ink-tertiary">
        <X size={12} /> Skipped
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

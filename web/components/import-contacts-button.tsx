"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { CheckCircle2, Cloud, Loader2, Upload, X } from "lucide-react";
import { cn } from "@/lib/cn";
import type { ParsedContact } from "@/lib/contacts";

// One preview row = a parsed contact + the user's per-row decisions (include /
// kind). The user reviews BEFORE anything is written, so importing a whole
// address book doesn't blindly create ledger counterparties.
type Row = ParsedContact & { include: boolean };

type ImportResult = {
  created: number;
  updated: number;
  skipped: number;
  total: number;
};

export function ImportContactsButton({
  workspaceId,
  contactOpsAvailable = false
}: {
  workspaceId: string;
  contactOpsAvailable?: boolean;
}) {
  const router = useRouter();
  const fileInput = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<Row[]>([]);
  const [filename, setFilename] = useState<string>("");
  const [parsing, setParsing] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);

  function reset() {
    setRows([]);
    setFilename("");
    setParsing(false);
    setImporting(false);
    setError(null);
    setResult(null);
    if (fileInput.current) fileInput.current.value = "";
  }

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setError(null);
    setParsing(true);
    setResult(null);
    try {
      const content = await file.text();
      const res = await fetch(
        `/api/workspaces/${workspaceId}/counterparties/parse`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: file.name, content })
        }
      );
      if (!res.ok) {
        const b = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(b.detail ?? "Could not read that file");
      }
      const data = (await res.json()) as { rows: ParsedContact[] };
      setFilename(file.name);
      setRows(data.rows.map((r) => ({ ...r, include: true })));
      if (data.rows.length === 0) {
        setError("No contacts found in that file.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setParsing(false);
    }
  }

  async function pullFromContactOps() {
    setError(null);
    setParsing(true);
    setResult(null);
    try {
      const res = await fetch(
        `/api/workspaces/${workspaceId}/counterparties/sync-contact-ops`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
      );
      if (!res.ok) {
        const b = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(b.detail ?? "Contact-Ops sync failed");
      }
      const data = (await res.json()) as { rows: ParsedContact[] };
      setFilename("Contact-Ops");
      setRows(data.rows.map((r) => ({ ...r, include: true })));
      if (data.rows.length === 0) {
        setError("No contacts came back from Contact-Ops.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setParsing(false);
    }
  }

  function setRow(i: number, patch: Partial<Row>) {
    setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }

  const includedCount = rows.filter((r) => r.include).length;

  async function doImport() {
    setError(null);
    setImporting(true);
    try {
      const payload = rows
        .filter((r) => r.include)
        .map((r) => ({
          display_name: r.display_name,
          kind: r.suggested_kind,
          email: r.email,
          phone: r.phone,
          address: r.address,
          company: r.company,
          person_name: r.person_name,
          source: r.source
        }));
      const res = await fetch(
        `/api/workspaces/${workspaceId}/counterparties/import`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rows: payload })
        }
      );
      if (!res.ok) {
        const b = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(b.detail ?? "Import failed");
      }
      setResult((await res.json()) as ImportResult);
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setImporting(false);
    }
  }

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) reset();
      }}
    >
      <Dialog.Trigger asChild>
        <button className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-brand-600">
          <Upload size={14} /> Import contacts
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-canvas/80 backdrop-blur-sm" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex max-h-[85vh] w-full max-w-2xl -translate-x-1/2 -translate-y-1/2 flex-col rounded-lg border border-default bg-surface shadow-card focus:outline-none">
          <div className="flex items-center justify-between border-b border-subtle px-6 py-4">
            <Dialog.Title className="text-base font-semibold">
              Import contacts
            </Dialog.Title>
            <Dialog.Close className="rounded p-1 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary">
              <X size={16} />
            </Dialog.Close>
          </div>

          {result ? (
            <div className="space-y-4 px-6 py-6">
              <div className="flex items-start gap-3 rounded-md border border-credit/40 bg-credit/[0.06] px-4 py-3">
                <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-credit" />
                <div className="text-sm">
                  <p className="font-semibold text-ink-primary">
                    Imported {result.created} new
                    {result.updated > 0 ? `, updated ${result.updated}` : ""}.
                  </p>
                  <p className="mt-1 text-ink-secondary">
                    {result.skipped > 0
                      ? `${result.skipped} skipped (no name or duplicate).`
                      : "All set."}
                  </p>
                </div>
              </div>
              <button
                onClick={() => {
                  setOpen(false);
                  reset();
                }}
                className="w-full rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white hover:bg-brand-600"
              >
                Done
              </button>
            </div>
          ) : (
            <>
              <div className="border-b border-subtle px-6 py-4">
                <Dialog.Description className="text-sm leading-relaxed text-ink-secondary">
                  Upload a <strong>.vcf</strong> (macOS / iPhone / Google /
                  Outlook contacts) or a <strong>.csv</strong>. Nothing is saved
                  until you review and confirm below — tag each as customer or
                  vendor, or untick to skip.
                </Dialog.Description>
                {contactOpsAvailable ? (
                  <div className="mt-3 rounded-md border border-brand-500/30 bg-brand-500/[0.06] px-4 py-3">
                    <div className="flex items-center justify-between gap-3">
                      <p className="text-sm text-ink-secondary">
                        <strong className="text-ink-primary">Recommended:</strong>{" "}
                        pull straight from Contact-Ops (your synced iCloud /
                        Microsoft / Gmail contacts) — no file needed.
                      </p>
                      <button
                        onClick={pullFromContactOps}
                        disabled={parsing}
                        className={cn(
                          "inline-flex shrink-0 items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-brand-600",
                          parsing && "cursor-not-allowed opacity-60"
                        )}
                      >
                        <Cloud size={14} /> Pull from Contact-Ops
                      </button>
                    </div>
                  </div>
                ) : null}

                <div className="mt-3 flex items-center gap-3">
                  <input
                    ref={fileInput}
                    type="file"
                    accept=".vcf,.csv,text/vcard,text/csv"
                    onChange={onFile}
                    className="block text-sm text-ink-secondary file:mr-3 file:rounded-md file:border-0 file:bg-elevated file:px-3 file:py-1.5 file:text-sm file:text-ink-primary hover:file:bg-white/10"
                  />
                  {parsing ? (
                    <Loader2 size={16} className="animate-spin text-ink-tertiary" />
                  ) : null}
                </div>
                {filename && rows.length > 0 ? (
                  <p className="mt-2 text-xs text-ink-tertiary">
                    {rows.length} contact{rows.length === 1 ? "" : "s"} found in{" "}
                    <span className="font-mono">{filename}</span> — {includedCount}{" "}
                    selected.
                  </p>
                ) : null}
              </div>

              {rows.length > 0 ? (
                <div className="min-h-0 flex-1 overflow-y-auto px-6 py-3">
                  <table className="w-full text-sm">
                    <thead className="sticky top-0 bg-surface text-left text-[11px] uppercase tracking-wider text-ink-tertiary">
                      <tr>
                        <th className="w-8 py-1.5"></th>
                        <th className="py-1.5 pr-2">Name</th>
                        <th className="py-1.5 pr-2">Email</th>
                        <th className="py-1.5">Kind</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-subtle">
                      {rows.map((r, i) => (
                        <tr
                          key={i}
                          className={cn(!r.include && "opacity-40")}
                        >
                          <td className="py-1.5">
                            <input
                              type="checkbox"
                              checked={r.include}
                              onChange={(e) =>
                                setRow(i, { include: e.target.checked })
                              }
                              className="h-4 w-4 accent-brand-500"
                            />
                          </td>
                          <td className="py-1.5 pr-2">
                            <div className="font-medium text-ink-primary">
                              {r.display_name}
                            </div>
                            {r.company && r.company !== r.display_name ? (
                              <div className="text-xs text-ink-tertiary">
                                {r.person_name}
                              </div>
                            ) : null}
                          </td>
                          <td className="py-1.5 pr-2 text-ink-secondary">
                            {r.email ?? (
                              <span className="text-ink-disabled">—</span>
                            )}
                          </td>
                          <td className="py-1.5">
                            <select
                              value={r.suggested_kind}
                              onChange={(e) =>
                                setRow(i, { suggested_kind: e.target.value })
                              }
                              disabled={!r.include}
                              className="rounded border border-default bg-canvas px-2 py-1 text-xs focus:border-brand-500 focus:outline-none"
                            >
                              <option value="customer">Customer</option>
                              <option value="vendor">Vendor</option>
                              <option value="both">Both</option>
                            </select>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}

              {error ? (
                <div className="mx-6 mb-2 rounded-md border border-debit/40 bg-debit/[0.06] px-3 py-2 text-sm text-debit">
                  {error}
                </div>
              ) : null}

              <div className="flex items-center justify-end gap-2 border-t border-subtle px-6 py-4">
                <Dialog.Close className="rounded-md border border-strong bg-surface px-4 py-2 text-sm text-ink-primary hover:bg-elevated">
                  Cancel
                </Dialog.Close>
                <button
                  onClick={doImport}
                  disabled={importing || includedCount === 0}
                  className={cn(
                    "inline-flex items-center gap-2 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-600",
                    (importing || includedCount === 0) &&
                      "cursor-not-allowed opacity-60"
                  )}
                >
                  {importing ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : null}
                  {importing
                    ? "Importing…"
                    : `Import ${includedCount || ""} contact${includedCount === 1 ? "" : "s"}`}
                </button>
              </div>
            </>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

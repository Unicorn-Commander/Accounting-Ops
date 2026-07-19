"use client";

import { useState, useTransition } from "react";
import {
  AlertTriangle,
  Download,
  FileCode2,
  Info,
  Loader2,
} from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import {
  downloadFrom,
  efileXmlProxyUrl,
  generateEfilePackage,
  type EfilePackageResult,
} from "@/lib/forms";

export type EfilePanelProps = {
  workspaceId: string;
  entityId: string;
  wpId: string;
  taxYear: number | null;
};

/** Coerce a manifest scalar into a short displayable string. */
function manifestValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

export function EfilePanel({
  workspaceId,
  entityId,
  wpId,
  taxYear,
}: EfilePanelProps) {
  const [generating, startGenerate] = useTransition();
  const [result, setResult] = useState<EfilePackageResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  function handleGenerate() {
    if (generating) return;
    setError(null);
    setDownloadError(null);
    startGenerate(async () => {
      try {
        const r = await generateEfilePackage(workspaceId, entityId, wpId);
        setResult(r);
      } catch (e) {
        setError(
          e instanceof Error ? e.message : "Could not generate the e-file package"
        );
      }
    });
  }

  async function handleDownload() {
    setDownloadError(null);
    setDownloading(true);
    try {
      const filename = `efile-1040-${taxYear ?? ""}.xml`.replace(/-+/g, "-");
      await downloadFrom(
        efileXmlProxyUrl(workspaceId, entityId, wpId, "attachment"),
        filename
      );
    } catch (e) {
      setDownloadError(e instanceof Error ? e.message : "Download failed");
    } finally {
      setDownloading(false);
    }
  }

  // Pull a couple of well-known manifest fields for the summary, but render the
  // whole manifest generically so nothing is silently hidden.
  const sha256 = result ? manifestValue(result.manifest["sha256"]) : "";
  const sha256Short = sha256 && sha256 !== "—" ? `${sha256.slice(0, 16)}…` : sha256;

  return (
    <Card padded={false} className="overflow-hidden">
      <div className="flex items-center gap-2 border-b border-subtle px-4 py-3">
        <FileCode2 size={15} className="shrink-0 text-brand-400" />
        <h3 className="text-sm font-semibold">E-file package (Form 1040)</h3>
      </div>

      <div className="space-y-4 p-4">
        <p className="text-[11px] leading-relaxed text-ink-tertiary">
          Generate a MeF-style XML package for this return. This builds the file
          only — it is <strong>not</strong> transmitted to the IRS.
        </p>

        <div className="flex items-center gap-3">
          <button
            onClick={handleGenerate}
            disabled={generating}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-elevated disabled:text-ink-disabled",
              generating && "cursor-wait"
            )}
          >
            {generating ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <FileCode2 size={13} />
            )}
            {result ? "Regenerate package" : "Generate e-file package"}
          </button>
          {error && (
            <span className="text-[11px] text-debit" title={error}>
              {error}
            </span>
          )}
        </div>

        {result && (
          <div className="space-y-3">
            {/* Manifest summary */}
            <div className="rounded-md border border-default bg-elevated/40 px-3 py-2.5">
              <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                Manifest
              </p>
              <dl className="mt-1.5 space-y-1">
                {Object.entries(result.manifest).map(([k, v]) => (
                  <div key={k} className="flex items-baseline justify-between gap-3">
                    <dt className="shrink-0 text-[11px] text-ink-tertiary">{k}</dt>
                    <dd className="truncate text-right font-mono text-[11px] text-ink-secondary">
                      {k === "sha256" ? sha256Short : manifestValue(v)}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>

            {/* Warnings */}
            {result.warnings.length > 0 && (
              <div className="flex items-start gap-2 rounded-md border border-pending/40 bg-pending/[0.06] px-3 py-2.5">
                <AlertTriangle size={13} className="mt-0.5 shrink-0 text-pending" />
                <div className="min-w-0">
                  <p className="text-[11px] font-medium text-pending">
                    {result.warnings.length}{" "}
                    {result.warnings.length === 1 ? "warning" : "warnings"}
                  </p>
                  <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[11px] leading-relaxed text-pending/90">
                    {result.warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                </div>
              </div>
            )}

            {/* The honest "generated, not transmitted" note (verbatim). */}
            <div className="flex items-start gap-2 rounded-md border border-brand-500/30 bg-brand-500/[0.06] px-3 py-2.5">
              <Info size={13} className="mt-0.5 shrink-0 text-brand-400" />
              <div className="min-w-0 text-[11px] leading-relaxed text-ink-secondary">
                <p className="font-semibold text-ink-primary">
                  Generated, NOT transmitted. Live IRS filing is gated on your
                  firm&apos;s EFIN.
                </p>
                {result.note && <p className="mt-1">{result.note}</p>}
              </div>
            </div>

            {/* Download */}
            <div className="flex items-center gap-3">
              <button
                onClick={handleDownload}
                disabled={downloading}
                className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs font-medium text-ink-secondary transition hover:text-ink-primary disabled:cursor-not-allowed disabled:opacity-50"
              >
                {downloading ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : (
                  <Download size={13} />
                )}
                {downloading ? "Preparing…" : "Download XML"}
              </button>
              {downloadError && (
                <span className="text-[11px] text-debit" title={downloadError}>
                  {downloadError}
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}

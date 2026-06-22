"use client";

// Route-level error boundary for /documents.
//
// A render error anywhere in the documents segment (the uploader, or the
// uploaded-documents list during a post-upload router.refresh()) is contained
// here as a friendly, recoverable card — instead of escaping to the global
// boundary and white-screening the whole app with "Application error: a
// client-side exception has occurred". The uploaded files are already stored
// server-side, so a reload always recovers.

import { useEffect } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

export default function DocumentsError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Surface for diagnosis; never crash the page.
    console.error("[documents] render error:", error);
  }, [error]);

  return (
    <div className="mx-auto max-w-md px-6 py-16 text-center">
      <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-pending/15 text-pending">
        <AlertTriangle size={22} />
      </div>
      <h2 className="text-sm font-semibold text-ink-primary">
        This page hit a snag
      </h2>
      <p className="mt-1.5 text-xs leading-relaxed text-ink-tertiary">
        Your uploaded documents are safe — they&apos;re stored and queued for
        review. Reloading shows the latest.
        {error?.digest ? (
          <span className="mt-2 block font-mono text-[10px] text-ink-disabled">
            ref: {error.digest}
          </span>
        ) : null}
      </p>
      <button
        type="button"
        onClick={() => reset()}
        className="mt-5 inline-flex items-center gap-1.5 rounded-lg bg-brand-500/15 px-3 py-1.5 text-xs font-medium text-brand-400 transition hover:bg-brand-500/25"
      >
        <RefreshCw size={13} /> Reload
      </button>
    </div>
  );
}

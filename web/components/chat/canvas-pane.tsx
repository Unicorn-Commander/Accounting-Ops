"use client";

import { Copy, FileText, Hammer, X } from "lucide-react";
import { useState } from "react";
import { MarkdownBlock } from "./message-bubble";
import { cn } from "@/lib/cn";
import type { CanvasArtifact } from "./types";

/**
 * Side pane that previews a substantial tool-result artifact. Opens
 * automatically when a tool returns a markdown / json / text payload
 * longer than ~1K bytes; user can close or pin.
 *
 * Renders by kind:
 *  - markdown — react-markdown
 *  - json     — formatted code block
 *  - text     — pre-formatted block
 */
export function CanvasPane({
  artifact,
  onClose
}: {
  artifact: CanvasArtifact | null;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  if (!artifact) return null;

  async function copy() {
    if (!artifact) return;
    try {
      await navigator.clipboard.writeText(artifact.text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // permissions may block; surface nothing — the button is just a nicety
    }
  }

  return (
    <aside
      className={cn(
        "flex h-full flex-col overflow-hidden rounded-lg border border-default bg-surface",
        "shadow-card"
      )}
      aria-label="Tool-result canvas"
    >
      <header className="flex items-center gap-2 border-b border-subtle bg-elevated/60 px-3.5 py-2.5">
        <Hammer size={14} className="text-agent-500" />
        <div className="min-w-0">
          <p className="truncate text-xs font-semibold text-ink-primary">
            {artifact.toolName}
          </p>
          <p className="text-[10px] text-ink-tertiary">
            {artifact.artifactKind.toUpperCase()} · {humanBytes(artifact.artifactSize)}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-1">
          <button
            type="button"
            onClick={copy}
            className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[11px] text-ink-secondary hover:bg-white/[0.05] hover:text-ink-primary"
            title="Copy artifact"
          >
            <Copy size={11} />
            {copied ? "Copied" : "Copy"}
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close canvas"
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-ink-tertiary hover:bg-white/[0.05] hover:text-ink-primary"
          >
            <X size={14} />
          </button>
        </div>
      </header>
      <div className="flex-1 overflow-auto px-4 py-4">
        {artifact.artifactKind === "markdown" ? (
          <MarkdownBlock text={artifact.text} />
        ) : artifact.artifactKind === "json" ? (
          <pre className="font-mono text-[11px] text-ink-primary whitespace-pre-wrap break-words">
            {tryPrettyJson(artifact.text)}
          </pre>
        ) : (
          <pre className="font-mono text-[11px] text-ink-primary whitespace-pre-wrap break-words">
            {artifact.text}
          </pre>
        )}
      </div>
      <footer className="border-t border-subtle bg-elevated/40 px-3.5 py-2 text-[10px] text-ink-tertiary">
        <FileText size={10} className="-mt-0.5 mr-1 inline-block" />
        Canvas previews are read-only. Use the chat composer to ask follow-ups.
      </footer>
    </aside>
  );
}

function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function tryPrettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

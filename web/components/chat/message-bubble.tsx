"use client";

import { useState } from "react";
import {
  Bot,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  StickyNote,
  User,
  Wrench
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/cn";
import type { RenderedMessage, ToolEvent } from "./types";

/**
 * Single chat bubble (user OR assistant). Assistant bubbles also render
 * a list of inline tool-call cards (collapsible). Streaming bubbles show
 * a typing indicator after any in-flight text.
 */
export function MessageBubble({
  message,
  agentLabel,
  onOpenArtifact,
  onSaveAsMemo,
  saveDisabled = false
}: {
  message: RenderedMessage;
  agentLabel?: string;
  onOpenArtifact?: (te: ToolEvent) => void;
  onSaveAsMemo?: (m: RenderedMessage) => void;
  saveDisabled?: boolean;
}) {
  const isUser = message.kind === "user";
  return (
    <div
      className={cn(
        "group flex w-full gap-3",
        isUser ? "flex-row-reverse" : "flex-row"
      )}
    >
      <div
        className={cn(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-full ring-1 ring-inset",
          isUser
            ? "bg-brand-500/15 text-brand-300 ring-brand-500/40"
            : "bg-elevated text-agent-500 ring-agent-500/30"
        )}
      >
        {isUser ? <User size={14} /> : <Bot size={14} />}
      </div>
      <div
        className={cn(
          "flex max-w-[min(80ch,calc(100%-3.5rem))] flex-col gap-2",
          isUser ? "items-end text-right" : "items-start text-left"
        )}
      >
        {!isUser && agentLabel && (
          <p className="text-[10px] font-medium uppercase tracking-wider text-agent-500">
            {agentLabel}
            {message.kind === "assistant" && message.model && (
              <span className="ml-2 text-ink-tertiary normal-case tracking-normal">
                {message.model.replace(/^anthropic\//, "")}
              </span>
            )}
          </p>
        )}
        {message.kind === "assistant" && message.toolEvents.length > 0 && (
          <div className="w-full space-y-1.5">
            {message.toolEvents.map((te) => (
              <ToolCallCard key={te.id} event={te} onOpenArtifact={onOpenArtifact} />
            ))}
          </div>
        )}
        {(message.kind === "user" || message.text || message.kind === "assistant") && (
          <div
            className={cn(
              "rounded-lg px-4 py-3 font-reasoning text-sm leading-relaxed",
              "[overflow-wrap:anywhere]",
              isUser
                ? "bg-brand-500/10 text-ink-primary"
                : "border-l-2 border-agent-500/60 bg-elevated text-ink-primary"
            )}
          >
            {isUser ? (
              <div className="whitespace-pre-wrap">{message.text}</div>
            ) : message.text ? (
              <MarkdownBlock text={message.text} />
            ) : message.kind === "assistant" && message.streaming ? (
              <TypingDots />
            ) : null}
            {message.kind === "assistant" && message.streaming && message.text && (
              <span className="ml-1 inline-block h-3 w-1.5 translate-y-0.5 animate-pulse bg-agent-500" />
            )}
          </div>
        )}
        {message.kind === "assistant" && !message.streaming && (message.text || message.toolEvents.length > 0) && (
          <div className="flex items-center gap-2 text-[11px] text-ink-tertiary">
            {message.cancelled && (
              <span className="rounded-sm bg-pending/15 px-1.5 py-0.5 text-pending">
                cancelled
              </span>
            )}
            {message.stopReason && message.stopReason !== "end_turn" && !message.cancelled && (
              <span className="rounded-sm bg-elevated px-1.5 py-0.5">
                {message.stopReason}
              </span>
            )}
            {onSaveAsMemo && (
              <button
                type="button"
                disabled={saveDisabled}
                onClick={() => onSaveAsMemo(message)}
                className="inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-ink-tertiary hover:bg-elevated hover:text-ink-secondary disabled:opacity-40 disabled:cursor-not-allowed"
                title="Save this response as a CPA review memo"
              >
                <StickyNote size={11} /> Save as memo
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function TypingDots() {
  return (
    <span className="inline-flex items-center gap-1 text-ink-tertiary" aria-label="Thinking">
      <span className="h-1.5 w-1.5 rounded-full bg-agent-500/70 animate-pulse" />
      <span
        className="h-1.5 w-1.5 rounded-full bg-agent-500/70 animate-pulse"
        style={{ animationDelay: "150ms" }}
      />
      <span
        className="h-1.5 w-1.5 rounded-full bg-agent-500/70 animate-pulse"
        style={{ animationDelay: "300ms" }}
      />
    </span>
  );
}

const PROSE_CLASSES = cn(
  "text-sm text-ink-primary leading-relaxed",
  "[&_h1]:text-base [&_h1]:font-semibold [&_h1]:mt-3 [&_h1]:mb-2",
  "[&_h2]:text-sm [&_h2]:font-semibold [&_h2]:mt-3 [&_h2]:mb-1.5",
  "[&_h3]:text-[13px] [&_h3]:font-semibold [&_h3]:mt-2 [&_h3]:mb-1",
  "[&_p]:mb-2 [&_p:last-child]:mb-0",
  "[&_ul]:mb-2 [&_ul]:pl-5 [&_ul]:list-disc",
  "[&_ol]:mb-2 [&_ol]:pl-5 [&_ol]:list-decimal",
  "[&_li]:mb-0.5",
  "[&_strong]:font-semibold [&_em]:italic",
  "[&_a]:text-brand-400 [&_a]:underline [&_a]:underline-offset-2",
  "[&_code]:font-mono [&_code]:text-[11px] [&_code]:bg-elevated [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded",
  "[&_pre]:font-mono [&_pre]:text-[11px] [&_pre]:bg-canvas/60 [&_pre]:p-3 [&_pre]:rounded-md [&_pre]:mb-2 [&_pre]:overflow-x-auto [&_pre]:border [&_pre]:border-subtle",
  "[&_pre_code]:bg-transparent [&_pre_code]:p-0",
  "[&_table]:w-full [&_table]:text-xs [&_table]:mb-2 [&_table]:border-collapse",
  "[&_th]:bg-canvas/60 [&_th]:font-semibold [&_th]:px-2 [&_th]:py-1.5 [&_th]:text-left [&_th]:border [&_th]:border-subtle",
  "[&_td]:px-2 [&_td]:py-1 [&_td]:border [&_td]:border-subtle [&_td]:align-top",
  "[&_blockquote]:border-l-4 [&_blockquote]:border-pending [&_blockquote]:pl-3 [&_blockquote]:py-0.5 [&_blockquote]:bg-pending/[0.05] [&_blockquote]:text-ink-secondary [&_blockquote]:mb-2"
);

export function MarkdownBlock({ text }: { text: string }) {
  return (
    <div className={PROSE_CLASSES}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

function ToolCallCard({
  event,
  onOpenArtifact
}: {
  event: ToolEvent;
  onOpenArtifact?: (te: ToolEvent) => void;
}) {
  const [open, setOpen] = useState(false);
  const hasResult = event.result !== undefined;
  const hasArtifact = !!event.artifactKind && (event.artifactSize ?? 0) >= 1024;

  return (
    <div className="overflow-hidden rounded-md border border-subtle bg-canvas/60 text-xs">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 hover:bg-white/[0.03]"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <Wrench size={11} className="text-agent-500/80" />
        <span className="font-mono text-[11px] text-ink-secondary">{event.name}</span>
        <span className="ml-1 truncate font-mono text-[10px] text-ink-tertiary">
          ({summarizeArgs(event.args)})
        </span>
        {hasResult ? (
          <span className="ml-auto text-[10px] text-credit">done</span>
        ) : (
          <span className="ml-auto inline-flex items-center gap-1 text-[10px] text-ink-tertiary">
            <span className="h-1 w-1 animate-pulse rounded-full bg-agent-500" />
            running
          </span>
        )}
      </button>
      {open && (
        <div className="space-y-2 border-t border-subtle px-2.5 py-2 font-mono text-[10px] text-ink-secondary">
          <div>
            <p className="mb-1 text-[9px] uppercase tracking-wider text-ink-tertiary">
              args
            </p>
            <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded bg-canvas/80 p-2 text-[10px]">
              {prettyJson(event.args)}
            </pre>
          </div>
          {hasResult && (
            <div>
              <div className="mb-1 flex items-center justify-between">
                <p className="text-[9px] uppercase tracking-wider text-ink-tertiary">
                  result
                </p>
                {hasArtifact && onOpenArtifact && (
                  <button
                    type="button"
                    onClick={() => onOpenArtifact(event)}
                    className="inline-flex items-center gap-1 rounded-sm bg-brand-500/10 px-1.5 py-0.5 text-[10px] text-brand-300 hover:bg-brand-500/20"
                  >
                    <ExternalLink size={10} /> Open in canvas
                  </button>
                )}
              </div>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-canvas/80 p-2 text-[10px]">
                {summarizeResult(event.result)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function summarizeArgs(args: Record<string, unknown>): string {
  const keys = Object.keys(args ?? {});
  if (keys.length === 0) return "no args";
  return keys
    .map((k) => `${k}=${shortValue(args[k])}`)
    .slice(0, 4)
    .join(", ");
}

function shortValue(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") {
    if (v.length > 32) return JSON.stringify(v.slice(0, 32) + "…");
    return JSON.stringify(v);
  }
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return `[${v.length}]`;
  return "{…}";
}

function prettyJson(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function summarizeResult(v: unknown): string {
  // MCP results come as { content: [{type, text}, ...] }. Extract text
  // items for readability.
  if (v && typeof v === "object" && "content" in (v as object)) {
    const arr = (v as { content?: Array<{ type?: string; text?: string }> }).content;
    if (Array.isArray(arr)) {
      const text = arr
        .map((item) => (typeof item === "string" ? item : item?.text ?? ""))
        .filter(Boolean)
        .join("\n\n");
      if (text) {
        const clipped = text.length > 1200 ? text.slice(0, 1200) + "…" : text;
        return clipped;
      }
    }
  }
  return prettyJson(v);
}

import { ArrowUp, Bot, MessageSquare, Paperclip, User } from "lucide-react";
import { cn } from "@/lib/cn";
import type { ChatMessage } from "@/lib/agent-chat";

/** Message list — shared by the full /chat page and the chat popout. */
export function ChatThread({
  messages,
  className,
  compact = false,
}: {
  messages: ChatMessage[];
  className?: string;
  compact?: boolean;
}) {
  if (messages.length === 0) {
    return (
      <div
        className={cn(
          "flex flex-col items-center justify-center gap-3 text-center",
          compact ? "p-6" : "p-10",
          className
        )}
      >
        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-agent-500/10 text-agent-500 ring-1 ring-inset ring-agent-500/30">
          <MessageSquare size={18} />
        </div>
        <div>
          <p className="text-sm font-medium text-ink-primary">No messages yet</p>
          <p className="mt-1 max-w-xs text-[11px] leading-relaxed text-ink-tertiary">
            Ask anything below to start a new chat. The orchestrator will route your
            question to the right specialist agent.
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className={cn("space-y-5 overflow-y-auto", compact ? "p-4" : "p-6", className)}>
      {messages.map((msg, i) => (
        <div key={i} className={cn("flex gap-3", msg.role === "user" && "flex-row-reverse")}>
          <div
            className={cn(
              "flex h-7 w-7 shrink-0 items-center justify-center rounded-full ring-1 ring-inset",
              msg.role === "agent"
                ? "bg-elevated text-agent-500 ring-agent-500/30"
                : "bg-brand-500/20 text-brand-400 ring-brand-500/40"
            )}
          >
            {msg.role === "agent" ? <Bot size={14} /> : <User size={14} />}
          </div>
          <div className={cn("max-w-[80%] space-y-1", msg.role === "user" && "items-end text-right")}>
            {msg.role === "agent" && msg.agent && (
              <p className="text-[11px] font-medium uppercase tracking-wider text-agent-500">
                {msg.agent}
              </p>
            )}
            <div
              className={cn(
                "whitespace-pre-line rounded-lg px-4 py-3 font-reasoning text-sm leading-relaxed",
                msg.role === "agent"
                  ? "border-l-2 border-agent-500/60 bg-elevated text-ink-primary"
                  : "bg-brand-500/10 text-ink-primary"
              )}
            >
              {msg.body}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

/** Composer (textarea + attach + send) — shared by page + popout. */
export function ChatComposer({
  compact = false,
  showRuntimeNote = true,
}: {
  compact?: boolean;
  showRuntimeNote?: boolean;
}) {
  return (
    <div className={cn("border-t border-subtle", compact ? "p-3" : "p-4")}>
      <div className="relative">
        <textarea
          rows={2}
          placeholder={
            compact
              ? "Ask the agents anything…"
              : "Ask the agents anything — try 'What's my burn this quarter?' or 'Categorize today's Stripe payouts'"
          }
          className="w-full resize-none rounded-lg border border-default bg-canvas px-4 py-3 pr-24 text-sm placeholder:text-ink-disabled focus:border-brand-500 focus:outline-none"
        />
        <div className="absolute right-2 top-2 flex items-center gap-1.5">
          <button
            type="button"
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
            title="Attach file"
            aria-label="Attach file"
          >
            <Paperclip size={14} />
          </button>
          <button
            type="button"
            aria-label="Send message"
            className="inline-flex h-7 items-center gap-1 rounded-md bg-brand-500 px-2.5 text-[11px] font-medium text-white shadow-brand-glow hover:bg-brand-600"
          >
            <ArrowUp size={14} />
          </button>
        </div>
      </div>
      {showRuntimeNote && (
        <p className="mt-2 text-[10px] text-ink-tertiary">
          Runtime: Tier 1 local Qwen on midboy1. Escalates to Claude Opus 4.7 on Tier 3.
        </p>
      )}
    </div>
  );
}

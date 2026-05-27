"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, Maximize2, MessageSquare, X } from "lucide-react";
import { ChatComposer, ChatThread } from "@/components/agent-chat-thread";
import { CHAT_MESSAGES } from "@/lib/agent-chat";

// Dockable / poppable agent-chat widget. Chat is now the thing that pops out
// (agent activity moved to a first-class inline dashboard section).
//
// Behavior:
//  - Collapsed: a launcher pill docked bottom-right.
//  - Open: a floating chat panel. The page stays interactive (non-blocking
//    popout, not a true modal), but Escape closes it and focus moves in on
//    open / returns to the launcher on close for keyboard users.
//  - "Open full chat" link routes to the /chat page for the roomier layout.

export function AgentChatPopout({ defaultOpen = false }: { defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const panelRef = useRef<HTMLDivElement>(null);
  const launcherRef = useRef<HTMLButtonElement>(null);

  const close = useCallback(() => setOpen(false), []);

  // Escape to close + move focus into the panel when it opens.
  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, close]);

  // Return focus to the launcher after closing (keyboard affordance).
  const wasOpen = useRef(open);
  useEffect(() => {
    if (wasOpen.current && !open) launcherRef.current?.focus();
    wasOpen.current = open;
  }, [open]);

  if (!open) {
    return (
      <button
        ref={launcherRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Open agent chat"
        aria-expanded={false}
        className="fixed bottom-4 right-4 z-40 inline-flex items-center gap-2 rounded-full border border-default bg-elevated/95 px-3.5 py-2.5 text-xs font-medium text-ink-secondary shadow-ticker backdrop-blur transition hover:bg-elevated hover:text-ink-primary"
      >
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full rounded-full bg-agent-500 opacity-75 animate-pulse-soft" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-agent-500" />
        </span>
        <MessageSquare size={14} />
        Chat with agents
      </button>
    );
  }

  return (
    <aside
      ref={panelRef}
      role="dialog"
      aria-modal="false"
      aria-label="Agent chat"
      tabIndex={-1}
      className="fixed bottom-4 right-4 z-40 flex max-h-[min(36rem,calc(100vh-2rem))] w-96 max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-lg border border-default bg-elevated/95 shadow-ticker backdrop-blur outline-none animate-fade-in"
    >
      <header className="flex items-center gap-2 border-b border-subtle px-3 py-2.5">
        <Bot size={15} className="text-agent-500" />
        <p className="text-xs font-semibold">Chat with your agents</p>
        <span className="ml-auto inline-flex items-center gap-1.5 font-mono text-[10px] text-ink-tertiary">
          <span className="h-1.5 w-1.5 rounded-full bg-credit shadow-agent-glow" />3 online
        </span>
        <Link
          href="/chat"
          onClick={close}
          title="Open full chat"
          aria-label="Open full chat page"
          className="ml-1 rounded p-0.5 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
        >
          <Maximize2 size={13} />
        </Link>
        <button
          type="button"
          onClick={close}
          aria-label="Close agent chat"
          className="rounded p-0.5 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
        >
          <X size={14} />
        </button>
      </header>

      <ChatThread messages={CHAT_MESSAGES} compact className="flex-1" />

      <ChatComposer compact showRuntimeNote={false} />
    </aside>
  );
}

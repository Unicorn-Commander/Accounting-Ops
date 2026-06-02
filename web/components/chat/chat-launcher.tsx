"use client";

// ChatLauncher — the single client entry point for the secondary chat
// surfaces, mounted once by the (server) AppShell. It owns:
//
//   1. A launcher pill docked bottom-right when everything is closed.
//   2. A compact FLOATING window (bottom-right) — the default surface.
//   3. A docked SIDEBAR panel (right edge, full height) — toggled from the
//      floating window's "dock" button.
//   4. A POP-OUT button on both that does window.open(...) to /chat/window,
//      resuming the same session via ?session=&entity=.
//
// All three render the SAME <EmbeddedChat> engine, which reuses the shared
// chat protocol (useChatStream + Composer + MessageBubble + PersonaPicker).
//
// Open state + surface mode persist to localStorage so the chat follows the
// user across pages (mirrors the nav sidebar's collapse-cookie behavior).
//
// Best-effort by construction: if no workspace/entity is provisioned, the
// component renders nothing — it must never break the host page.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Bot,
  ExternalLink,
  Maximize2,
  MessageSquare,
  PanelRightOpen,
  PictureInPicture2,
  X
} from "lucide-react";
import { EmbeddedChat } from "./embedded-chat";
import type { ChatPersona } from "./types";

type Surface = "closed" | "float" | "dock";

const OPEN_KEY = "aops_chat_open"; // "1" while a surface is open
const MODE_KEY = "aops_chat_mode"; // "float" | "dock"

export type ChatLauncherProps = {
  workspaceId: string;
  entityId: string;
  entityName?: string;
  personas: ChatPersona[];
  defaultPersonaSlug: string;
};

export function ChatLauncher({
  workspaceId,
  entityId,
  entityName,
  personas,
  defaultPersonaSlug
}: ChatLauncherProps) {
  const [surface, setSurface] = useState<Surface>("closed");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [hydrated, setHydrated] = useState(false);

  const panelRef = useRef<HTMLDivElement>(null);
  const launcherRef = useRef<HTMLButtonElement>(null);

  // Hydrate persisted open/mode from localStorage (client-only; SSR renders
  // the closed pill to avoid a hydration mismatch).
  useEffect(() => {
    let mode: Surface = "float";
    try {
      const m = window.localStorage.getItem(MODE_KEY);
      if (m === "dock" || m === "float") mode = m;
      const open = window.localStorage.getItem(OPEN_KEY) === "1";
      setSurface(open ? mode : "closed");
    } catch {
      // localStorage blocked (private mode) — just start closed.
    }
    setHydrated(true);
  }, []);

  const persist = useCallback((next: Surface) => {
    try {
      window.localStorage.setItem(OPEN_KEY, next === "closed" ? "0" : "1");
      if (next !== "closed") window.localStorage.setItem(MODE_KEY, next);
    } catch {
      /* ignore */
    }
  }, []);

  const setSurfacePersisted = useCallback(
    (next: Surface) => {
      setSurface(next);
      persist(next);
    },
    [persist]
  );

  const close = useCallback(() => setSurfacePersisted("closed"), [setSurfacePersisted]);

  // Escape closes the active surface; move focus into the panel on open and
  // back to the launcher on close (keyboard affordance — same as the old popout).
  useEffect(() => {
    if (surface === "closed") return;
    panelRef.current?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [surface, close]);

  const wasOpen = useRef(false);
  useEffect(() => {
    const open = surface !== "closed";
    if (wasOpen.current && !open) launcherRef.current?.focus();
    wasOpen.current = open;
  }, [surface]);

  // Pop the current conversation out into a dedicated browser window. We resume
  // the same session + entity via query params on the minimal /chat/window route.
  const popOut = useCallback(() => {
    const params = new URLSearchParams({ entity: entityId });
    if (sessionId) params.set("session", sessionId);
    const url = `/chat/window?${params.toString()}`;
    window.open(
      url,
      `aops-chat-${sessionId ?? "new"}`,
      "popup=yes,width=440,height=720,menubar=no,toolbar=no,location=no,status=no"
    );
    // Collapse the in-page surface; the conversation now lives in the popup.
    close();
  }, [entityId, sessionId, close]);

  // Don't render anything host-breaking before hydration, but DO render the
  // closed pill on the server so there's no layout pop-in.
  const showPill = !hydrated || surface === "closed";

  return (
    <>
      {showPill && (
        <button
          ref={launcherRef}
          type="button"
          onClick={() => setSurfacePersisted("float")}
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
      )}

      {hydrated && surface === "float" && (
        <aside
          ref={panelRef}
          role="dialog"
          aria-modal="false"
          aria-label="Agent chat"
          tabIndex={-1}
          className="fixed bottom-4 right-4 z-40 flex max-h-[min(36rem,calc(100vh-2rem))] w-96 max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-lg border border-default bg-elevated/95 shadow-ticker backdrop-blur outline-none animate-fade-in"
        >
          <ChatHeader
            onDock={() => setSurfacePersisted("dock")}
            onPopOut={popOut}
            onClose={close}
          />
          <EmbeddedChat
            workspaceId={workspaceId}
            entityId={entityId}
            entityName={entityName}
            personas={personas}
            defaultPersonaSlug={defaultPersonaSlug}
            onSessionChange={setSessionId}
            className="flex-1 min-h-0"
          />
        </aside>
      )}

      {hydrated && surface === "dock" && (
        <aside
          ref={panelRef}
          role="dialog"
          aria-modal="false"
          aria-label="Agent chat"
          tabIndex={-1}
          className="fixed right-0 top-14 z-40 flex h-[calc(100vh-3.5rem)] w-[min(24rem,100vw)] flex-col overflow-hidden border-l border-default bg-surface shadow-ticker outline-none animate-slide-in-right"
        >
          <ChatHeader
            docked
            onDock={() => setSurfacePersisted("float")}
            onPopOut={popOut}
            onClose={close}
          />
          <EmbeddedChat
            workspaceId={workspaceId}
            entityId={entityId}
            entityName={entityName}
            personas={personas}
            defaultPersonaSlug={defaultPersonaSlug}
            onSessionChange={setSessionId}
            className="flex-1 min-h-0"
          />
        </aside>
      )}
    </>
  );
}

function ChatHeader({
  docked = false,
  onDock,
  onPopOut,
  onClose
}: {
  docked?: boolean;
  onDock: () => void;
  onPopOut: () => void;
  onClose: () => void;
}) {
  return (
    <header className="flex items-center gap-2 border-b border-subtle px-3 py-2.5">
      <Bot size={15} className="text-agent-500" />
      <p className="text-xs font-semibold">Chat with your agents</p>
      <div className="ml-auto flex items-center gap-0.5">
        <button
          type="button"
          onClick={onDock}
          title={docked ? "Float window" : "Dock to side"}
          aria-label={docked ? "Switch to floating window" : "Dock chat to the side"}
          className="rounded p-1 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
        >
          {docked ? <PictureInPicture2 size={14} /> : <PanelRightOpen size={14} />}
        </button>
        <button
          type="button"
          onClick={onPopOut}
          title="Open in a separate window"
          aria-label="Pop out chat to a separate browser window"
          className="rounded p-1 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
        >
          <ExternalLink size={14} />
        </button>
        <Link
          href="/chat"
          onClick={onClose}
          title="Open full chat page"
          aria-label="Open full chat page"
          className="rounded p-1 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
        >
          <Maximize2 size={13} />
        </Link>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close agent chat"
          className="rounded p-1 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
        >
          <X size={14} />
        </button>
      </div>
    </header>
  );
}

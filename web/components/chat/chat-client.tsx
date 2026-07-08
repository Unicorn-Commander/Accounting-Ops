"use client";

// Top-level client component for the /chat page. Manages:
//  - sidebar of past sessions (refetched after each new send)
//  - persona picker (read-only after first message)
//  - message thread (history + in-flight assistant turn)
//  - canvas pane (auto-opens when a tool returns a substantial artifact)
//  - composer (Cmd+Enter to send, Stop to cancel)
//
// Routing: ?session=<sid> selects a session; ?persona=<slug> stages a new
// chat with that persona; ?taxYear=<n> binds a tax year on the next new
// chat. The page-level Server Component preloads sessions + personas and
// passes them in as initial props.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertCircle, Bot, History, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { ChatSidebar } from "./sidebar";
import { Composer } from "./composer";
import { PersonaPicker } from "./persona-picker";
import { MessageBubble } from "./message-bubble";
import { CanvasPane } from "./canvas-pane";
import { useChatStream } from "./use-chat-stream";
import {
  fromServerMessage,
  type CanvasArtifact,
  type ChatPersona,
  type ChatSessionSummary,
  type RenderedMessage,
  type ToolEvent
} from "./types";

type Entity = {
  id: string;
  display_name: string;
  slug: string;
};

export type ChatClientProps = {
  workspaceId: string;
  entities: Entity[];
  activeEntityId: string;
  defaultPersonaSlug: string;
  personas: ChatPersona[];
  initialSessions: ChatSessionSummary[];
  initialActiveSessionId: string | null;
  // Optional: hide the sidebar (used by /setup which is a focused single-pane flow).
  hideSidebar?: boolean;
  // Optional: lock persona picker (used by /setup which forces setup-assistant).
  lockPersona?: boolean;
  // Optional: callback fired when the assistant streams ANY tool call.
  // Used by /setup to listen for `mark_intake_complete` and redirect.
  onAssistantToolCall?: (event: {
    name: string;
    args: Record<string, unknown>;
    result?: unknown;
  }) => void;
};

const DEFAULT_TAX_YEAR = new Date().getFullYear() - 1;

export function ChatClient({
  workspaceId,
  entities,
  activeEntityId,
  defaultPersonaSlug,
  personas,
  initialSessions,
  initialActiveSessionId,
  hideSidebar = false,
  lockPersona = false,
  onAssistantToolCall
}: ChatClientProps) {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [sessions, setSessions] = useState<ChatSessionSummary[]>(initialSessions);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(
    initialActiveSessionId
  );
  const [stagedPersona, setStagedPersona] = useState<string>(
    searchParams.get("persona") ?? defaultPersonaSlug
  );
  const [stagedTaxYear, setStagedTaxYear] = useState<number>(
    Number(searchParams.get("taxYear")) || DEFAULT_TAX_YEAR
  );

  const [messages, setMessages] = useState<RenderedMessage[]>([]);
  const [activeSessionMeta, setActiveSessionMeta] = useState<{
    persona: string;
    entityId: string;
    taxYear: number | null;
    title: string;
  } | null>(null);

  const [loadingSession, setLoadingSession] = useState(false);
  const [pendingError, setPendingError] = useState<string | null>(null);

  const [draft, setDraft] = useState("");
  const [canvas, setCanvas] = useState<CanvasArtifact | null>(null);
  const [canvasPinned, setCanvasPinned] = useState(false);
  // Mobile-only sessions drawer (slides in from the left). Hidden when
  // hideSidebar is true (the /setup route).
  const [mobileSessionsOpen, setMobileSessionsOpen] = useState(false);

  const threadRef = useRef<HTMLDivElement>(null);
  const stream = useChatStream();
  // Referentially stable (useCallback []); safe to depend on without churning
  // loadSession. Used to drop a surfaced error when abandoning the current turn.
  const { clearError } = stream;

  // -------------------------------------------------------------------------
  // Load a session when the user clicks one (or on first mount if initial id).
  // -------------------------------------------------------------------------
  const loadSession = useCallback(
    async (sid: string) => {
      setLoadingSession(true);
      setPendingError(null);
      clearError();
      setMessages([]);
      setCanvas(null);
      setCanvasPinned(false);
      try {
        const res = await fetch(`/api/chat/workspaces/${workspaceId}/sessions/${sid}`, {
          cache: "no-store"
        });
        if (!res.ok) {
          throw new Error(`Failed to load session: HTTP ${res.status}`);
        }
        const data = (await res.json()) as {
          session: {
            id: string;
            entity_id: string;
            agent_persona: string | null;
            tax_year: number | null;
            title: string | null;
          };
          messages: import("@/lib/chat").ChatMessage[];
        };
        const hydrated = data.messages
          .map(fromServerMessage)
          .filter((m): m is RenderedMessage => m !== null);
        setMessages(hydrated);
        setActiveSessionMeta({
          persona: data.session.agent_persona ?? defaultPersonaSlug,
          entityId: data.session.entity_id,
          taxYear: data.session.tax_year,
          title: data.session.title ?? "Untitled conversation"
        });
      } catch (err) {
        setPendingError(err instanceof Error ? err.message : "Failed to load session");
      } finally {
        setLoadingSession(false);
      }
    },
    [workspaceId, defaultPersonaSlug, clearError]
  );

  useEffect(() => {
    if (activeSessionId) {
      loadSession(activeSessionId);
    } else {
      setMessages([]);
      setActiveSessionMeta(null);
    }
  }, [activeSessionId, loadSession]);

  // Sync the ?session= query string.
  useEffect(() => {
    const url = new URL(window.location.href);
    if (activeSessionId) {
      url.searchParams.set("session", activeSessionId);
    } else {
      url.searchParams.delete("session");
    }
    window.history.replaceState({}, "", url.toString());
  }, [activeSessionId]);

  // Mobile drawer: Escape closes it. Background scroll-lock so swipes don't
  // accidentally interact with the chat thread underneath.
  useEffect(() => {
    if (!mobileSessionsOpen) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setMobileSessionsOpen(false);
    }
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [mobileSessionsOpen]);

  // Auto-scroll to the bottom on new messages + streaming text.
  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages, stream.inflight]);

  // Auto-open the canvas when a new substantial artifact appears.
  useEffect(() => {
    if (canvasPinned) return;
    const inflight = stream.inflight;
    if (!inflight || inflight.kind !== "assistant") return;
    for (const te of inflight.toolEvents) {
      const art = artifactFromToolEvent(te);
      if (art && (!canvas || canvas.key !== art.key)) {
        setCanvas(art);
        break;
      }
    }
  }, [stream.inflight, canvas, canvasPinned]);

  // -------------------------------------------------------------------------
  // Send / cancel
  // -------------------------------------------------------------------------
  const refreshSessions = useCallback(async () => {
    try {
      const res = await fetch(
        `/api/chat/workspaces/${workspaceId}/sessions?limit=50`,
        { cache: "no-store" }
      );
      if (!res.ok) return;
      const data = (await res.json()) as { sessions: ChatSessionSummary[] };
      setSessions(data.sessions);
    } catch {
      // best-effort refresh
    }
  }, [workspaceId]);

  const send = useCallback(async () => {
    const text = draft.trim();
    if (!text) return;

    // 1. If no active session, create one bound to the staged persona + entity + year.
    let targetSessionId = activeSessionId;
    if (!targetSessionId) {
      try {
        const res = await fetch(
          `/api/chat/workspaces/${workspaceId}/sessions`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              entity_id: activeEntityId,
              agent_persona: stagedPersona,
              tax_year: stagedTaxYear
            })
          }
        );
        if (!res.ok) {
          const detail = await res.text().catch(() => "");
          throw new Error(detail || `HTTP ${res.status}`);
        }
        const data = (await res.json()) as {
          session: ChatSessionSummary & { agent_persona: string | null };
        };
        targetSessionId = data.session.id;
        setSessions((prev) => [data.session, ...prev]);
        setActiveSessionId(data.session.id);
        setActiveSessionMeta({
          persona: data.session.agent_persona ?? stagedPersona,
          entityId: data.session.entity_id,
          taxYear: data.session.tax_year,
          title: data.session.title ?? "Untitled conversation"
        });
      } catch (err) {
        setPendingError(err instanceof Error ? err.message : "Failed to start chat");
        return;
      }
    }
    if (!targetSessionId) return;

    // 2. Append the user message immediately (optimistic).
    const userMsg: RenderedMessage = {
      kind: "user",
      id: `local-${Date.now()}`,
      text,
      createdAt: new Date().toISOString()
    };
    setMessages((prev) => [...prev, userMsg]);
    setDraft("");

    // 3. Start the SSE stream.
    // Buffer in-flight tool calls so we can fire `onAssistantToolCall` once
    // the corresponding tool_result frame arrives (with the response payload).
    const pendingToolCalls = new Map<string, { name: string; args: Record<string, unknown> }>();
    await stream.start({
      workspaceId,
      sessionId: targetSessionId,
      text,
      onEvent: (evt) => {
        if (!onAssistantToolCall) return;
        if (evt.type === "tool_call") {
          pendingToolCalls.set(evt.id, { name: evt.name, args: evt.args });
        } else if (evt.type === "tool_result") {
          const pending = pendingToolCalls.get(evt.id);
          if (pending) {
            onAssistantToolCall({
              name: pending.name,
              args: pending.args,
              result: evt.result
            });
            pendingToolCalls.delete(evt.id);
          }
        }
      },
      onStreamEnd: (final) => {
        // Move the in-flight bubble into the history.
        if (final && final.kind === "assistant") {
          setMessages((prev) => [...prev, final]);
        }
        stream.clear();
        refreshSessions();
      }
    });
  }, [
    draft,
    activeSessionId,
    workspaceId,
    activeEntityId,
    stagedPersona,
    stagedTaxYear,
    stream,
    refreshSessions,
    onAssistantToolCall
  ]);

  const startNewChat = useCallback(() => {
    if (stream.streaming) {
      stream.cancel();
    }
    setActiveSessionId(null);
    setActiveSessionMeta(null);
    clearError();
    setMessages([]);
    setCanvas(null);
    setCanvasPinned(false);
    setStagedPersona(defaultPersonaSlug);
    setStagedTaxYear(DEFAULT_TAX_YEAR);
    setDraft("");
  }, [defaultPersonaSlug, stream, clearError]);

  // -------------------------------------------------------------------------
  // Save as memo
  // -------------------------------------------------------------------------
  const saveAsMemo = useCallback(
    async (m: RenderedMessage) => {
      if (m.kind !== "assistant" || !activeSessionId) return;
      const topic = activeSessionMeta?.title ?? undefined;
      try {
        const res = await fetch(
          `/api/chat/workspaces/${workspaceId}/sessions/${activeSessionId}/save-as-memo`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message_id: m.id, topic })
          }
        );
        if (!res.ok) {
          const detail = await res.text().catch(() => "");
          throw new Error(detail || `HTTP ${res.status}`);
        }
        // Optimistic toast — we just inline the OK on the message.
        const data = (await res.json()) as {
          memo_id: string;
          tax_year: number;
          bucket?: string;
          open_items_url?: string;
        };
        const bucketLabel: Record<string, string> = {
          time_sensitive: "Time-sensitive",
          decision: "Decisions awaiting you",
          document_ask: "Documents to provide",
          informational: "Recent AI activity (informational)"
        };
        const sectionName = data.bucket
          ? bucketLabel[data.bucket] ?? "Open Items"
          : "Open Items";
        const openItemsLink = data.open_items_url || "/open-items";
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === m.id && msg.kind === "assistant"
              ? {
                  ...msg,
                  text:
                    msg.text +
                    `\n\n_Memo saved to **[${sectionName}](${openItemsLink})** ` +
                    `(id ${data.memo_id.slice(0, 8)})._`
                }
              : msg
          )
        );
      } catch (err) {
        setPendingError(err instanceof Error ? err.message : "Failed to save memo");
      }
    },
    [workspaceId, activeSessionId, activeSessionMeta]
  );

  // -------------------------------------------------------------------------
  // Derived UI state
  // -------------------------------------------------------------------------
  const isExistingSession = !!activeSessionId;
  const personaInUse = isExistingSession
    ? activeSessionMeta?.persona ?? stagedPersona
    : stagedPersona;
  const personaLabel =
    personas.find((p) => p.slug === personaInUse)?.display_name ?? personaInUse;
  const headerEntityName =
    entities.find((e) => e.id === (activeSessionMeta?.entityId ?? activeEntityId))?.display_name ??
    activeEntityId;
  const headerTaxYear = isExistingSession
    ? activeSessionMeta?.taxYear ?? null
    : stagedTaxYear;

  const allMessages: RenderedMessage[] = useMemo(() => {
    if (stream.inflight) return [...messages, stream.inflight];
    return messages;
  }, [messages, stream.inflight]);

  const showCanvas = !!canvas;

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------
  return (
    <div
      className={cn(
        "flex flex-col gap-4 lg:flex-row",
        // The /setup (hideSidebar) flow shares the page with the onboarding
        // hero + checklist, so the chat is shorter on mobile to avoid an
        // overlong first-run scroll; the standalone /chat page fills the view.
        hideSidebar
          ? "min-h-[26rem] h-[70vh] md:h-[calc(100vh-7rem)]"
          : "h-[calc(100vh-6.5rem)] min-h-[34rem] md:h-[calc(100vh-7rem)]"
      )}
    >
      {/* Left sidebar — hidden when caller wants a focused single-pane view (e.g. /setup) */}
      {!hideSidebar && (
        <ChatSidebar
          sessions={sessions}
          activeId={activeSessionId}
          onSelect={(id) => setActiveSessionId(id)}
          onNew={startNewChat}
          className="hidden w-64 shrink-0 lg:flex"
        />
      )}

      {/* Mobile sessions drawer — slides in from the left, < lg only */}
      {!hideSidebar && mobileSessionsOpen && (
        <div
          className="fixed inset-0 z-50 lg:hidden"
          role="dialog"
          aria-modal="true"
          aria-label="Chat sessions"
          id="chat-mobile-sessions"
        >
          {/* Backdrop — tap to close. Distinct from the drawer panel so swipes
              on the panel don't bubble to it. */}
          <button
            type="button"
            aria-label="Close sessions"
            onClick={() => setMobileSessionsOpen(false)}
            className="absolute inset-0 cursor-default bg-black/55 backdrop-blur-sm transition-opacity"
            tabIndex={-1}
          />
          <aside
            className={cn(
              "relative flex h-full w-[min(86vw,22rem)] flex-col bg-surface shadow-xl",
              "animate-slide-in-left"
            )}
          >
            <div className="flex items-center justify-between border-b border-subtle px-3 py-2">
              <p className="text-xs font-semibold text-ink-primary">
                Sessions
              </p>
              <button
                type="button"
                onClick={() => setMobileSessionsOpen(false)}
                aria-label="Close"
                className="inline-flex h-7 w-7 items-center justify-center rounded-md text-ink-secondary hover:bg-elevated focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
              >
                <X size={14} />
              </button>
            </div>
            <ChatSidebar
              sessions={sessions}
              activeId={activeSessionId}
              onSelect={(id) => {
                setActiveSessionId(id);
                setMobileSessionsOpen(false);
              }}
              onNew={() => {
                startNewChat();
                setMobileSessionsOpen(false);
              }}
              className="flex flex-1 rounded-none border-0"
            />
          </aside>
        </div>
      )}

      {/* Main pane */}
      <section
        className={cn(
          "flex h-full min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-default bg-surface"
        )}
      >
        <header className="flex flex-wrap items-center gap-2 border-b border-subtle bg-elevated/40 px-3 py-2 md:px-4 md:py-2.5">
          {/* Mobile-only "Sessions" hamburger — opens the slide-in drawer. */}
          {!hideSidebar && (
            <button
              type="button"
              onClick={() => setMobileSessionsOpen(true)}
              aria-label="Open chat sessions"
              aria-controls="chat-mobile-sessions"
              aria-expanded={mobileSessionsOpen}
              className={cn(
                "inline-flex h-8 w-8 items-center justify-center rounded-md border border-subtle bg-elevated text-ink-secondary hover:bg-surface focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500",
                "lg:hidden"
              )}
              title="Sessions (Esc to close)"
            >
              <History size={14} />
            </button>
          )}
          <Bot size={14} className="text-agent-500" />
          <PersonaPicker
            personas={personas}
            value={personaInUse}
            onChange={(slug) => setStagedPersona(slug)}
            disabled={isExistingSession || lockPersona}
          />
          <span className="text-[10px] text-ink-tertiary">·</span>
          <span className="text-xs text-ink-secondary">{headerEntityName}</span>
          {headerTaxYear && (
            <>
              <span className="text-[10px] text-ink-tertiary">·</span>
              {isExistingSession ? (
                <span className="rounded-sm bg-elevated px-1.5 py-0.5 text-[10px] font-medium text-ink-secondary">
                  TY {headerTaxYear}
                </span>
              ) : (
                <YearChips value={stagedTaxYear} onChange={setStagedTaxYear} />
              )}
            </>
          )}
          {!isExistingSession && (
            <span className="ml-auto text-[10px] text-ink-tertiary">
              New conversation
            </span>
          )}
          {isExistingSession && activeSessionMeta && (
            <span className="ml-auto truncate text-[10px] text-ink-tertiary md:max-w-[16rem]">
              {activeSessionMeta.title}
            </span>
          )}
        </header>

        <div
          ref={threadRef}
          className="flex-1 space-y-5 overflow-y-auto px-4 py-5 md:px-6"
        >
          {loadingSession && (
            <p className="text-center text-xs text-ink-tertiary">Loading conversation…</p>
          )}
          {!loadingSession && allMessages.length === 0 && (
            <EmptyStateHero personaLabel={personaLabel} />
          )}
          {allMessages.map((m) => (
            <MessageBubble
              key={m.id}
              message={m}
              agentLabel={m.kind === "assistant" ? personaLabel : undefined}
              onOpenArtifact={(te) => {
                const art = artifactFromToolEvent(te);
                if (art) {
                  setCanvas(art);
                  setCanvasPinned(true);
                }
              }}
              onSaveAsMemo={
                m.kind === "assistant" && !m.streaming && m.id !== "pending"
                  ? saveAsMemo
                  : undefined
              }
              saveDisabled={false}
            />
          ))}
          {pendingError && (
            <div className="flex items-start gap-2 rounded-md border border-debit/40 bg-debit/[0.05] px-3 py-2 text-xs text-debit">
              <AlertCircle size={13} className="mt-0.5" />
              <p>{pendingError}</p>
              <button
                type="button"
                onClick={() => setPendingError(null)}
                className="ml-auto text-debit/80 hover:text-debit"
              >
                dismiss
              </button>
            </div>
          )}
          {stream.error && (
            <div className="flex items-start gap-2 rounded-md border border-debit/40 bg-debit/[0.05] px-3 py-2 text-xs text-debit">
              <AlertCircle size={13} className="mt-0.5" />
              <p>{stream.error}</p>
            </div>
          )}
        </div>

        <Composer
          value={draft}
          onChange={setDraft}
          onSubmit={send}
          onCancel={() => stream.cancel()}
          streaming={stream.streaming}
          disabled={loadingSession}
          placeholder={
            isExistingSession
              ? `Reply to ${personaLabel}…`
              : `Ask ${personaLabel} about ${headerEntityName}…`
          }
        />
      </section>

      {/* Right canvas */}
      {showCanvas && (
        <div className="hidden h-full w-[28rem] shrink-0 xl:block">
          <CanvasPane
            artifact={canvas}
            onClose={() => {
              setCanvas(null);
              setCanvasPinned(false);
            }}
          />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function artifactFromToolEvent(te: ToolEvent): CanvasArtifact | null {
  if (!te.result || !te.artifactKind) return null;
  if (te.artifactKind === null) return null;
  const text = extractTextFromMcpResult(te.result);
  if (!text) return null;
  return {
    key: te.id,
    toolName: te.name,
    artifactKind: te.artifactKind,
    artifactSize: te.artifactSize ?? text.length,
    text
  };
}

function extractTextFromMcpResult(result: unknown): string {
  if (!result || typeof result !== "object") return "";
  const r = result as { content?: Array<unknown> };
  if (Array.isArray(r.content)) {
    const parts: string[] = [];
    for (const item of r.content) {
      if (typeof item === "string") parts.push(item);
      else if (item && typeof item === "object" && "text" in (item as object)) {
        const t = (item as { text?: string }).text;
        if (typeof t === "string") parts.push(t);
      }
    }
    return parts.join("\n\n");
  }
  // JSON-shaped result: pretty-print.
  try {
    return JSON.stringify(result, null, 2);
  } catch {
    return "";
  }
}

function YearChips({
  value,
  onChange
}: {
  value: number;
  onChange: (n: number) => void;
}) {
  const current = new Date().getFullYear();
  const years = [current - 1, current - 2, current - 3];
  return (
    <div className="flex items-center gap-1">
      {years.map((y) => (
        <button
          key={y}
          type="button"
          onClick={() => onChange(y)}
          className={cn(
            "rounded-sm px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider",
            value === y
              ? "bg-brand-500/15 text-brand-300"
              : "text-ink-tertiary hover:bg-elevated hover:text-ink-secondary"
          )}
        >
          TY{y}
        </button>
      ))}
    </div>
  );
}

function EmptyStateHero({ personaLabel }: { personaLabel: string }) {
  return (
    <div className="mx-auto max-w-md py-10 text-center text-sm text-ink-secondary">
      <Bot size={28} className="mx-auto text-agent-500" />
      <p className="mt-3 font-medium text-ink-primary">
        Chat with {personaLabel}
      </p>
      <p className="mt-1 text-xs text-ink-tertiary">
        Ask anything about your books or this entity&apos;s tax year. The agent
        will pull live data through the MCP tools and cite its sources.
      </p>
      <div className="mt-4 space-y-1 text-left text-[11px] text-ink-tertiary">
        <p>Try:</p>
        <ul className="space-y-1 rounded-md border border-subtle bg-elevated/50 p-3">
          <li>· &quot;Pull the May Mercury transactions and flag anything over $5,000&quot;</li>
          <li>· &quot;What deductions are still open on the 2023 1120?&quot;</li>
          <li>· &quot;Draft a memo for the §382 ownership analysis&quot;</li>
        </ul>
      </div>
    </div>
  );
}

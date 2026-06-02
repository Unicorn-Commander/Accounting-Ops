"use client";

// EmbeddedChat — the compact, height-flexible chat engine shared by every
// secondary chat surface (the bottom-right floating window, the docked
// sidebar panel, and the standalone /chat/window pop-out).
//
// It deliberately reuses the SAME building blocks as the full-page
// `ChatClient` — `useChatStream` (SSE), `Composer`, `MessageBubble`,
// `PersonaPicker`, and the `types.ts` reducers — so there is exactly one
// chat protocol implementation. It does NOT fork ChatClient (the /chat page
// keeps its own sessions sidebar + canvas pane + URL sync, none of which make
// sense in a 384px box).
//
// Behavior mirror of ChatClient's send/load flow, minus the desktop-only
// chrome:
//   - On first send with no active session, POST a new session bound to the
//     chosen persona + entity + tax year, then open the SSE stream.
//   - Selecting/resuming an existing session loads its history first.
//   - The persona picker locks once a session exists (persona is immutable
//     server-side after creation), exactly like the full page.
//
// The host (launcher / window page) owns the frame (size, header buttons);
// this component fills whatever box it's given via `h-full`.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, Bot } from "lucide-react";
import { cn } from "@/lib/cn";
import { Composer } from "./composer";
import { MessageBubble } from "./message-bubble";
import { PersonaPicker } from "./persona-picker";
import { useChatStream } from "./use-chat-stream";
import {
  fromServerMessage,
  type ChatPersona,
  type ChatSessionSummary,
  type RenderedMessage
} from "./types";
import type { ChatMessage } from "@/lib/chat";

const DEFAULT_TAX_YEAR = new Date().getFullYear() - 1;

export type EmbeddedChatProps = {
  workspaceId: string;
  /** Single entity the chat is bound to (chat tools require a single entity_id). */
  entityId: string;
  entityName?: string;
  personas: ChatPersona[];
  defaultPersonaSlug: string;
  /** Resume an existing session (pop-out / deep link). When set, history loads on mount. */
  initialSessionId?: string | null;
  /** Tax year to bind on the next NEW session. Defaults to current year - 1. */
  defaultTaxYear?: number;
  /** Show the inline persona picker in the header row. Off for the tightest layouts. */
  showPersonaPicker?: boolean;
  /** Fired whenever the active session id changes (null → created, or loaded).
   *  Used by the launcher to build the "pop out to window" URL. */
  onSessionChange?: (sessionId: string | null) => void;
  className?: string;
};

export function EmbeddedChat({
  workspaceId,
  entityId,
  entityName,
  personas,
  defaultPersonaSlug,
  initialSessionId = null,
  defaultTaxYear = DEFAULT_TAX_YEAR,
  showPersonaPicker = true,
  onSessionChange,
  className
}: EmbeddedChatProps) {
  const [activeSessionId, setActiveSessionId] = useState<string | null>(
    initialSessionId
  );
  const [stagedPersona, setStagedPersona] = useState<string>(defaultPersonaSlug);
  const [messages, setMessages] = useState<RenderedMessage[]>([]);
  const [loadingSession, setLoadingSession] = useState(false);
  const [pendingError, setPendingError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const threadRef = useRef<HTMLDivElement>(null);
  const stream = useChatStream();

  // Notify the host when the session id changes (created or resumed).
  useEffect(() => {
    onSessionChange?.(activeSessionId);
  }, [activeSessionId, onSessionChange]);

  // Load history when resuming an existing session.
  const loadSession = useCallback(
    async (sid: string) => {
      setLoadingSession(true);
      setPendingError(null);
      setMessages([]);
      try {
        const res = await fetch(
          `/api/chat/workspaces/${workspaceId}/sessions/${sid}`,
          { cache: "no-store" }
        );
        if (!res.ok) throw new Error(`Failed to load session: HTTP ${res.status}`);
        const data = (await res.json()) as {
          session: { agent_persona: string | null };
          messages: ChatMessage[];
        };
        const hydrated = data.messages
          .map(fromServerMessage)
          .filter((m): m is RenderedMessage => m !== null);
        setMessages(hydrated);
        if (data.session.agent_persona) setStagedPersona(data.session.agent_persona);
      } catch (err) {
        setPendingError(
          err instanceof Error ? err.message : "Failed to load conversation"
        );
      } finally {
        setLoadingSession(false);
      }
    },
    [workspaceId]
  );

  // On mount (and whenever the resumed id prop changes), load it once.
  useEffect(() => {
    if (initialSessionId) {
      setActiveSessionId(initialSessionId);
      loadSession(initialSessionId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSessionId]);

  // Auto-scroll to the latest message / streamed delta.
  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages, stream.inflight]);

  const isExistingSession = !!activeSessionId;
  const personaInUse = stagedPersona;
  const personaLabel =
    personas.find((p) => p.slug === personaInUse)?.display_name ?? personaInUse;

  const send = useCallback(async () => {
    const text = draft.trim();
    if (!text) return;

    // 1. Ensure a session exists (create lazily, bound to persona + entity + year).
    let targetSessionId = activeSessionId;
    if (!targetSessionId) {
      try {
        const res = await fetch(
          `/api/chat/workspaces/${workspaceId}/sessions`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              entity_id: entityId,
              agent_persona: stagedPersona,
              tax_year: defaultTaxYear
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
        setActiveSessionId(data.session.id);
        if (data.session.agent_persona) setStagedPersona(data.session.agent_persona);
      } catch (err) {
        setPendingError(
          err instanceof Error ? err.message : "Failed to start chat"
        );
        return;
      }
    }
    if (!targetSessionId) return;

    // 2. Optimistic user bubble.
    const userMsg: RenderedMessage = {
      kind: "user",
      id: `local-${Date.now()}`,
      text,
      createdAt: new Date().toISOString()
    };
    setMessages((prev) => [...prev, userMsg]);
    setDraft("");

    // 3. Stream the assistant turn.
    await stream.start({
      workspaceId,
      sessionId: targetSessionId,
      text,
      onStreamEnd: (final) => {
        if (final && final.kind === "assistant") {
          setMessages((prev) => [...prev, final]);
        }
        stream.clear();
      }
    });
  }, [draft, activeSessionId, workspaceId, entityId, stagedPersona, defaultTaxYear, stream]);

  const allMessages: RenderedMessage[] = useMemo(() => {
    if (stream.inflight) return [...messages, stream.inflight];
    return messages;
  }, [messages, stream.inflight]);

  return (
    <div className={cn("flex min-h-0 flex-col", className)}>
      {showPersonaPicker && (
        <div className="flex flex-wrap items-center gap-2 border-b border-subtle bg-elevated/40 px-3 py-2">
          <Bot size={14} className="text-agent-500" />
          <PersonaPicker
            personas={personas}
            value={personaInUse}
            onChange={(slug) => setStagedPersona(slug)}
            disabled={isExistingSession}
            compact
          />
          {entityName && (
            <>
              <span className="text-[10px] text-ink-tertiary">·</span>
              <span className="truncate text-xs text-ink-secondary">{entityName}</span>
            </>
          )}
        </div>
      )}

      <div
        ref={threadRef}
        className="flex-1 space-y-4 overflow-y-auto px-3 py-4"
      >
        {loadingSession && (
          <p className="text-center text-xs text-ink-tertiary">Loading conversation…</p>
        )}
        {!loadingSession && allMessages.length === 0 && (
          <EmbeddedEmptyState personaLabel={personaLabel} />
        )}
        {allMessages.map((m) => (
          <MessageBubble
            key={m.id}
            message={m}
            agentLabel={m.kind === "assistant" ? personaLabel : undefined}
          />
        ))}
        {pendingError && (
          <ErrorRow message={pendingError} onDismiss={() => setPendingError(null)} />
        )}
        {stream.error && <ErrorRow message={stream.error} />}
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
            : `Ask ${personaLabel}${entityName ? ` about ${entityName}` : ""}…`
        }
      />
    </div>
  );
}

function ErrorRow({
  message,
  onDismiss
}: {
  message: string;
  onDismiss?: () => void;
}) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-debit/40 bg-debit/[0.05] px-3 py-2 text-xs text-debit">
      <AlertCircle size={13} className="mt-0.5 shrink-0" />
      <p className="min-w-0 [overflow-wrap:anywhere]">{message}</p>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          className="ml-auto shrink-0 text-debit/80 hover:text-debit"
        >
          dismiss
        </button>
      )}
    </div>
  );
}

function EmbeddedEmptyState({ personaLabel }: { personaLabel: string }) {
  return (
    <div className="mx-auto max-w-xs py-6 text-center text-sm text-ink-secondary">
      <Bot size={24} className="mx-auto text-agent-500" />
      <p className="mt-2 font-medium text-ink-primary">Chat with {personaLabel}</p>
      <p className="mt-1 text-xs text-ink-tertiary">
        Ask about your books or this entity&apos;s tax year. DRAFT memos route to
        the CPA queue — nothing changes without an explicit approval.
      </p>
    </div>
  );
}

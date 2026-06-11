"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import * as Dialog from "@radix-ui/react-dialog";
import { Search, Sparkles } from "lucide-react";
import { useChatStream } from "@/components/chat/use-chat-stream";

const GOTO_ITEMS = [
  { label: "Approvals queue", href: "/approvals", hue: "43 96% 56%", shortcut: "↵" },
  { label: "Taxes", href: "/taxes", hue: "12 85% 63%", shortcut: "⌘T" },
  { label: "Chat with your agents", href: "/chat", hue: "188 90% 56%", shortcut: "⌘J" },
];

export function CommandK({
  workspaceId,
  entityId,
}: {
  workspaceId?: string;
  entityId?: string;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [asked, setAsked] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const stream = useChatStream();

  // ⌘K / Esc
  useEffect(() => {
    function handler(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((p) => !p);
      }
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  // Reset transient state whenever the palette closes.
  useEffect(() => {
    if (!open) {
      setAsked(false);
      setCreateError(null);
      sessionIdRef.current = null;
      stream.clear();
    }
  }, [open, stream]);

  const ask = useCallback(async () => {
    const text = query.trim();
    if (!text) return;
    if (!workspaceId || !entityId) {
      setCreateError("Open a workspace and entity to ask the CFO agent.");
      setAsked(true);
      return;
    }
    setAsked(true);
    setCreateError(null);
    try {
      // Create a fresh CFO chat session (mirrors chat-client's contract).
      let sessionId = sessionIdRef.current;
      if (!sessionId) {
        const res = await fetch(`/api/chat/workspaces/${workspaceId}/sessions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ entity_id: entityId, agent_persona: null, tax_year: null }),
        });
        if (!res.ok) {
          throw new Error((await res.text().catch(() => "")) || `HTTP ${res.status}`);
        }
        const data = (await res.json()) as { session: { id: string } };
        sessionId = data.session.id;
        sessionIdRef.current = sessionId;
      }
      await stream.start({ workspaceId, sessionId, text });
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Failed to reach the agent.");
    }
  }, [query, workspaceId, entityId, stream]);

  const handleGoto = useCallback(
    (href: string) => {
      setOpen(false);
      router.push(href);
    },
    [router]
  );

  const answer = stream.inflight?.text ?? "";

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <button className="kbtn" id="kbtn" onClick={() => setOpen(true)} aria-label="Ask anything (⌘K)">
        <Search size={14} />
        Ask anything <kbd>⌘K</kbd>
      </button>

      <Dialog.Portal>
        <Dialog.Overlay className="kover open" />
        <Dialog.Content className="kpal" onOpenAutoFocus={(e) => e.preventDefault()}>
          <Dialog.Title className="sr-only">Ask anything</Dialog.Title>
          {/* Input */}
          <div className="kin">
            <Sparkles size={18} className="text-[var(--brand-300)]" />
            <input
              id="kinput"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void ask();
                }
              }}
              placeholder="Ask anything about your books…"
              autoFocus
            />
            <span className="esc">esc</span>
          </div>

          {/* Answer — streamed from the real CFO agent. Only shown after asking. */}
          {asked && (
            <div className="kans">
              <div className="who">
                <span className="d" />
                CFO agent · answering from your live books
              </div>
              {createError ? (
                <p style={{ color: "var(--debit)" }}>{createError}</p>
              ) : (
                <p>
                  {answer}
                  {stream.streaming && answer === "" ? "Thinking…" : null}
                </p>
              )}
            </div>
          )}

          {/* Go to */}
          <div className="ksec">
            <h5>Go to</h5>
            {GOTO_ITEMS.map((item) => (
              <div
                key={item.href}
                className="krow"
                style={{ "--ic": item.hue } as React.CSSProperties}
                onClick={() => handleGoto(item.href)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleGoto(item.href);
                }}
                tabIndex={0}
                role="button"
                aria-label={`Go to ${item.label}`}
              >
                <span className="ic">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" width="14" height="14">
                    <path d="M9 11l3 3 8-8" />
                    <path d="M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9" />
                  </svg>
                </span>
                {item.label}
                <span className="k">{item.shortcut}</span>
              </div>
            ))}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

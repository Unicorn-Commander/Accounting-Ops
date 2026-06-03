"use client";

// SetupClient — thin wrapper around ChatClient for the /setup surface.
//
// What this adds on top of ChatClient:
//   - Locks the persona to ``setup-assistant`` (no picker).
//   - Hides the chat-sessions sidebar (focused single-pane flow).
//   - Listens for the ``mark_intake_complete`` tool call and redirects to
//     /dashboard when fired.
//   - Optionally auto-seeds the first user message (e.g. "I want to add an
//     LLC") if the page passed a ``kickoffMessage`` — currently shown as a
//     hint in the empty state so the user can click-to-send without typing.
//   - Mobile-responsive: stacks vertically below `lg`.

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertCircle, CheckCircle2, Loader2, Sparkles } from "lucide-react";
import { ChatClient } from "@/components/chat/chat-client";
import { Card } from "@/components/primitives";
import { OnboardingHero } from "@/components/onboarding-hero";
import type {
  ChatPersona,
  ChatSessionSummary
} from "@/lib/chat";
import { SetupChecklist, type SetupChecklistHandle } from "./setup-checklist";

type Entity = { id: string; display_name: string; slug: string };

type Props = {
  workspaceId: string;
  workspaceName: string;
  entities: Entity[];
  // null when the workspace has 0 entities; SetupClient calls the bootstrap
  // endpoint on mount to provision a placeholder.
  activeEntityId: string | null;
  personas: ChatPersona[];
  initialSessions: ChatSessionSummary[];
  initialActiveSessionId: string | null;
  kickoffMessage: string | null;
  hasEntities: boolean;
  fromChat: boolean;
};

export function SetupClient({
  workspaceId,
  workspaceName,
  entities,
  activeEntityId,
  personas,
  initialSessions,
  initialActiveSessionId,
  kickoffMessage,
  hasEntities,
  fromChat
}: Props) {
  const router = useRouter();
  const [redirecting, setRedirecting] = useState(false);
  const [completion, setCompletion] = useState<{
    summary: string;
    nextSteps: string[];
  } | null>(null);

  // Bootstrap state — when there's no entity yet, hit the bootstrap endpoint
  // to create a "Setup Sandbox" placeholder. The Setup Assistant will replace
  // it with the user's real entity via the conversation.
  const [boundEntityId, setBoundEntityId] = useState<string | null>(activeEntityId);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);
  const [bootstrapping, setBootstrapping] = useState<boolean>(!activeEntityId);
  const [boundEntities, setBoundEntities] = useState<Entity[]>(entities);
  // Bumped to retry the bootstrap after a failure without a full page reload.
  const [bootstrapAttempt, setBootstrapAttempt] = useState(0);

  useEffect(() => {
    if (activeEntityId) return;
    let cancelled = false;
    (async () => {
      setBootstrapping(true);
      setBootstrapError(null);
      try {
        const res = await fetch(
          `/api/proxy/api/workspaces/${workspaceId}/setup/bootstrap`,
          { method: "POST", cache: "no-store" }
        );
        if (!res.ok) {
          const detail = await res.text().catch(() => "");
          throw new Error(detail || `HTTP ${res.status}`);
        }
        const data = (await res.json()) as {
          entity_id: string;
          display_name: string;
          kind: string;
          is_placeholder: boolean;
        };
        if (cancelled) return;
        setBoundEntityId(data.entity_id);
        // If a placeholder was created, surface it in the entities list so the
        // EntitySelector + chat header show something sensible.
        if (data.is_placeholder) {
          setBoundEntities((prev) =>
            prev.some((e) => e.id === data.entity_id)
              ? prev
              : [
                  ...prev,
                  {
                    id: data.entity_id,
                    display_name: data.display_name,
                    slug: "setup-sandbox"
                  }
                ]
          );
        }
      } catch (err) {
        if (cancelled) return;
        setBootstrapError(err instanceof Error ? err.message : "Bootstrap failed");
      } finally {
        if (!cancelled) setBootstrapping(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeEntityId, workspaceId, bootstrapAttempt]);

  // Ref to the checklist so we can forward EVERY assistant tool_call event,
  // not just `mark_intake_complete`. The checklist filters internally to the
  // ones that signify step completion.
  const checklistRef = useRef<SetupChecklistHandle | null>(null);

  // Listen for mark_intake_complete tool calls. When seen, surface a small
  // success card on top of the chat and redirect to /dashboard after a short
  // delay so the user can read the recap.
  const handleAssistantToolCall = useCallback(
    (event: { name: string; args: Record<string, unknown>; result?: unknown }) => {
      // Always notify the checklist; it filters internally.
      checklistRef.current?.noteToolEvent(event);
      if (event.name !== "mark_intake_complete") return;
      let summary = "Setup complete.";
      let nextSteps: string[] = [];
      // The args carry the assistant's intent; the result carries what the
      // server echoed back. Prefer result over args.
      const r = (event.result ?? {}) as {
        content?: Array<{ type?: string; text?: string }>;
      };
      // The MCP wire format wraps the dict result as a TextContent payload.
      // Try to parse the first text item back into JSON.
      try {
        const item = (r.content ?? []).find((c) => c?.type === "text" || c?.text);
        if (item?.text) {
          const parsed = JSON.parse(item.text) as {
            summary?: string;
            next_steps?: string[];
          };
          if (parsed.summary) summary = parsed.summary;
          if (Array.isArray(parsed.next_steps)) nextSteps = parsed.next_steps;
        }
      } catch {
        // Fall back to args if the result is not JSON.
        const a = event.args as { summary?: string; next_steps?: string[] };
        if (typeof a.summary === "string") summary = a.summary;
        if (Array.isArray(a.next_steps))
          nextSteps = a.next_steps.filter((s): s is string => typeof s === "string");
      }
      setCompletion({ summary, nextSteps });
    },
    []
  );

  useEffect(() => {
    if (!completion) return;
    setRedirecting(true);
    const t = setTimeout(() => {
      router.push("/dashboard");
      router.refresh();
    }, 4500);
    return () => clearTimeout(t);
  }, [completion, router]);

  return (
    <>
    {/* PRIMARY first-run path — dump documents + connect bank. The Setup
        Assistant chat below is the guided alternative. Shown once an entity is
        bound (the bootstrap resolves boundEntityId on mount for true first-run);
        the hero degrades to a "pick/await an entity" state if not yet bound. */}
    {!completion && (
      <OnboardingHero
        workspaceId={workspaceId}
        workspaceName={workspaceName}
        entityId={boundEntityId}
        entityName={
          boundEntities.find((e) => e.id === boundEntityId)?.display_name
        }
      />
    )}
    <div className="flex flex-col gap-3 md:grid md:grid-cols-[15rem_1fr] md:gap-4 lg:grid-cols-[17rem_1fr]">
      {/* Persistent setup checklist — left rail on md+, top strip on mobile */}
      <SetupChecklist
        ref={checklistRef}
        workspaceId={workspaceId}
        className="md:sticky md:top-3 md:self-start"
      />

      <div className="space-y-3 min-w-0">
      {/* Onboarding hint — what to expect (only shown before the first turn) */}
      {initialActiveSessionId === null && (
        <Card>
          <div className="flex flex-wrap items-center gap-2 text-xs text-ink-secondary">
            <Sparkles size={14} className="text-agent-500" />
            <span>
              The <strong>Setup Assistant</strong> will ask you 5-8 short
              questions about your business and provision an entity + chart of
              accounts. About 10-15 minutes.
            </span>
            {!hasEntities && (
              <span className="rounded-full bg-pending/15 px-2 py-0.5 text-[10px] font-medium text-pending">
                FIRST RUN
              </span>
            )}
            {fromChat && (
              <span className="rounded-full bg-brand-500/15 px-2 py-0.5 text-[10px] font-medium text-brand-300">
                FROM CHAT
              </span>
            )}
          </div>
          {kickoffMessage && (
            <p className="mt-3 text-xs text-ink-tertiary">
              Tip: start by saying{" "}
              <code className="rounded bg-elevated px-1.5 py-0.5 font-mono text-[11px]">
                {kickoffMessage}
              </code>
            </p>
          )}
        </Card>
      )}

      {/* Completion banner — replaces nothing; sits above the chat */}
      {completion && (
        <Card>
          <div className="flex items-start gap-3">
            <CheckCircle2 size={20} className="mt-0.5 shrink-0 text-credit" />
            <div className="flex-1">
              <h3 className="text-sm font-semibold text-credit">
                Setup complete
              </h3>
              <p className="mt-1 text-sm text-ink-secondary">{completion.summary}</p>
              {completion.nextSteps.length > 0 && (
                <ul className="mt-2 space-y-1 text-xs text-ink-secondary">
                  {completion.nextSteps.map((step, i) => (
                    <li key={i} className="flex items-start gap-2">
                      <span className="text-brand-400">→</span>
                      <span>{step}</span>
                    </li>
                  ))}
                </ul>
              )}
              <p className="mt-3 inline-flex items-center gap-1.5 text-xs text-ink-tertiary">
                {redirecting ? (
                  <>
                    <Loader2 size={12} className="animate-spin" />
                    Taking you to the dashboard…
                  </>
                ) : (
                  "Redirecting shortly…"
                )}
              </p>
            </div>
          </div>
        </Card>
      )}

      {/* Bootstrap error surface — clear copy + a one-click retry so a brand-new
          user isn't stranded on a failed first step. */}
      {bootstrapError && !bootstrapping && (
        <Card>
          <div className="flex items-start gap-3">
            <AlertCircle size={20} className="mt-0.5 shrink-0 text-debit" />
            <div className="flex-1">
              <h3 className="text-sm font-semibold text-debit">
                We couldn&apos;t create your first entity
              </h3>
              <p className="mt-1 text-sm text-ink-secondary">
                Something went wrong while setting up your workspace. This is
                usually temporary — try again, and if it keeps happening reach
                out from{" "}
                <a href="/help" className="text-brand-400 hover:underline">
                  /help
                </a>
                .
              </p>
              <p className="mt-2 break-words font-mono text-[11px] text-ink-tertiary">
                {bootstrapError}
              </p>
              <button
                type="button"
                onClick={() => setBootstrapAttempt((n) => n + 1)}
                className="mt-3 inline-flex items-center gap-1.5 rounded-md border border-subtle bg-elevated px-3 py-1.5 text-xs font-medium text-ink-secondary hover:bg-surface"
              >
                Try again
              </button>
            </div>
          </div>
        </Card>
      )}

      {/* While bootstrapping a placeholder entity, explain what's happening so a
          brand-new user isn't staring at a context-free spinner. */}
      {bootstrapping ? (
        <Card>
          <div className="flex items-start gap-3 py-4">
            <Loader2 size={18} className="mt-0.5 shrink-0 animate-spin text-brand-400" />
            <div>
              <p className="text-sm font-medium text-ink-primary">
                Creating your first entity…
              </p>
              <p className="mt-1 text-sm text-ink-secondary">
                Setting up a private workspace for your books. The Setup
                Assistant will walk you through the next few minutes — no
                accounting experience needed.
              </p>
            </div>
          </div>
        </Card>
      ) : boundEntityId ? (
        <ChatClient
          workspaceId={workspaceId}
          entities={boundEntities}
          activeEntityId={boundEntityId}
          defaultPersonaSlug="setup-assistant"
          personas={personas}
          initialSessions={initialSessions}
          initialActiveSessionId={initialActiveSessionId}
          hideSidebar={true}
          lockPersona={true}
          onAssistantToolCall={handleAssistantToolCall}
        />
      ) : null}
      </div>
    </div>
    </>
  );
}

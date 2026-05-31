"use client";

import { useState } from "react";
import { Sparkles } from "lucide-react";
import { ChatComposer, ChatThread } from "@/components/agent-chat-thread";
import { PersonaPicker } from "@/components/persona-picker";
import type { Persona } from "@/lib/personas";
import { CHAT_MESSAGES } from "@/lib/agent-chat";

/** Client-side wrapper for the chat page so persona selection re-renders the
 *  agent label + (eventually) refetches the seed conversation per persona.
 *
 *  Today the actual chat thread is still the seeded mock conversation from
 *  agent-chat.ts so the UI is demoable on a fresh worktree. When the live
 *  SSE chat dispatcher lands, this is where the message list will rehydrate
 *  per session.
 */
export function ChatShell({
  personas,
  brigadeOnline,
  brigadeReason,
  initialSlug = "cfo-agent"
}: {
  personas: Persona[];
  brigadeOnline: boolean;
  brigadeReason: string;
  initialSlug?: string;
}) {
  const initial = personas.find((p) => p.slug === initialSlug) ?? personas[0];
  const [active, setActive] = useState<Persona | undefined>(initial);

  return (
    <div className="flex flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-subtle px-5 py-3">
        <Sparkles size={14} className="text-agent-500" />
        <span className="text-xs text-ink-tertiary">Persona</span>
        <PersonaPicker
          personas={personas}
          brigadeOnline={brigadeOnline}
          brigadeReason={brigadeReason}
          activeSlug={active?.slug}
          onSelect={setActive}
        />
        {active && (
          <div className="ml-auto flex flex-wrap items-center gap-1.5 text-[10px] text-ink-tertiary">
            <span className="font-mono">{active.tool_count} tools</span>
            {active.brigade_active && active.cross_app_tool_count > 0 && (
              <span className="rounded bg-agent-500/15 px-1.5 py-0.5 text-agent-500">
                +{active.cross_app_tool_count} cross-app
              </span>
            )}
            {active.default_model && (
              <span className="font-mono">{active.default_model}</span>
            )}
          </div>
        )}
      </div>
      <ChatThread messages={CHAT_MESSAGES} className="flex-1" />
      <ChatComposer />
    </div>
  );
}

"use client";

// React hook that opens an SSE stream against /api/chat/.../messages and
// reduces the events into a single in-flight assistant message. The hook
// returns:
//   - `streaming`: boolean — true between session-open and stop/error
//   - `start(text)`: POSTs the user message and begins streaming
//   - `cancel()`: tells the backend to stop and aborts the fetch
//   - `inflight`: the assistant message being built (or null when idle)
//   - `lastEvent`: the most recent SSE event (used for artifact-detection
//     in the parent component so we don't re-render the whole pane on
//     every text_delta)
//   - `error`: error string (or null)

import { useCallback, useRef, useState } from "react";
import type { RenderedMessage, StreamEvent, ToolEvent } from "./types";

export type StartArgs = {
  workspaceId: string;
  sessionId: string;
  text: string;
  onSessionOpen?: (e: Extract<StreamEvent, { type: "session" }>) => void;
  onStreamEnd?: (final: RenderedMessage | null, stop?: Extract<StreamEvent, { type: "stop" }>) => void;
  onEvent?: (e: StreamEvent) => void;
};

export function useChatStream() {
  const [inflight, setInflight] = useState<RenderedMessage | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const cancelTargetRef = useRef<{ workspaceId: string; sessionId: string } | null>(null);

  const start = useCallback(async (args: StartArgs) => {
    const { workspaceId, sessionId, text, onSessionOpen, onStreamEnd, onEvent } = args;
    if (abortRef.current) {
      abortRef.current.abort();
    }
    const ctl = new AbortController();
    abortRef.current = ctl;
    cancelTargetRef.current = { workspaceId, sessionId };
    setError(null);
    setStreaming(true);

    // Seed an empty assistant placeholder so the UI immediately shows the
    // streaming bubble + typing indicator.
    const placeholder: RenderedMessage = {
      kind: "assistant",
      id: "pending",
      text: "",
      toolEvents: [],
      streaming: true
    };
    setInflight(placeholder);

    let final: RenderedMessage | null = placeholder;
    let stopEvent: Extract<StreamEvent, { type: "stop" }> | undefined;

    try {
      const res = await fetch(
        `/api/chat/workspaces/${workspaceId}/sessions/${sessionId}/messages`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
          body: JSON.stringify({ text }),
          signal: ctl.signal
        }
      );
      if (!res.ok || !res.body) {
        const detail = await res.text().catch(() => "");
        throw new Error(detail || `HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // SSE frames are separated by a blank line. Parse all complete frames.
        let split: number;
        while ((split = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, split);
          buf = buf.slice(split + 2);

          // Each frame is { event: <name>, data: <json> }.
          let eventName = "message";
          const dataLines: string[] = [];
          for (const line of frame.split("\n")) {
            if (line.startsWith(":")) continue; // comment / heartbeat
            if (line.startsWith("event:")) {
              eventName = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              dataLines.push(line.slice(5).trimStart());
            }
          }
          if (dataLines.length === 0) continue;
          let data: unknown;
          try {
            data = JSON.parse(dataLines.join("\n"));
          } catch {
            continue;
          }
          const evt = { type: eventName, ...(data as object) } as StreamEvent;
          onEvent?.(evt);

          if (evt.type === "session") {
            // Re-key the placeholder to the assistant_message_id so the UI
            // can persist it cleanly after stream end.
            setInflight((prev) =>
              prev && prev.kind === "assistant"
                ? { ...prev, id: evt.assistant_message_id, model: evt.model }
                : prev
            );
            final = {
              kind: "assistant",
              id: evt.assistant_message_id,
              text: "",
              toolEvents: [],
              streaming: true,
              model: evt.model
            };
            onSessionOpen?.(evt);
          } else if (evt.type === "text_delta") {
            const delta = evt.text;
            setInflight((prev) => {
              if (!prev || prev.kind !== "assistant") return prev;
              return { ...prev, text: prev.text + delta };
            });
            if (final && final.kind === "assistant") {
              final = { ...final, text: final.text + delta };
            }
          } else if (evt.type === "tool_call") {
            const tc: ToolEvent = {
              id: evt.id,
              name: evt.name,
              args: evt.args
            };
            setInflight((prev) => {
              if (!prev || prev.kind !== "assistant") return prev;
              return { ...prev, toolEvents: [...prev.toolEvents, tc] };
            });
            if (final && final.kind === "assistant") {
              final = { ...final, toolEvents: [...final.toolEvents, tc] };
            }
          } else if (evt.type === "tool_result") {
            setInflight((prev) => {
              if (!prev || prev.kind !== "assistant") return prev;
              return {
                ...prev,
                toolEvents: prev.toolEvents.map((te) =>
                  te.id === evt.id
                    ? {
                        ...te,
                        result: evt.result,
                        artifactKind: evt.artifact_kind,
                        artifactSize: evt.artifact_size
                      }
                    : te
                )
              };
            });
            if (final && final.kind === "assistant") {
              final = {
                ...final,
                toolEvents: final.toolEvents.map((te) =>
                  te.id === evt.id
                    ? {
                        ...te,
                        result: evt.result,
                        artifactKind: evt.artifact_kind,
                        artifactSize: evt.artifact_size
                      }
                    : te
                )
              };
            }
          } else if (evt.type === "stop") {
            stopEvent = evt;
            setInflight((prev) => {
              if (!prev || prev.kind !== "assistant") return prev;
              return {
                ...prev,
                streaming: false,
                stopReason: evt.stop_reason,
                cancelled: evt.stop_reason === "cancelled"
              };
            });
            if (final && final.kind === "assistant") {
              final = {
                ...final,
                streaming: false,
                stopReason: evt.stop_reason,
                cancelled: evt.stop_reason === "cancelled"
              };
            }
          } else if (evt.type === "error") {
            setError(evt.message);
          }
        }
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg !== "BodyStreamBuffer was aborted" && msg !== "The user aborted a request.") {
        setError(msg);
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
      onStreamEnd?.(final, stopEvent);
    }
  }, []);

  const cancel = useCallback(async () => {
    // Always wire-fire a cancel to the server; aborting the fetch on the
    // client alone doesn't stop the LLM call upstream.
    const target = cancelTargetRef.current;
    if (target) {
      try {
        await fetch(
          `/api/chat/workspaces/${target.workspaceId}/sessions/${target.sessionId}/cancel`,
          { method: "POST" }
        );
      } catch {
        // Best-effort: even if cancel fails, abort the client fetch.
      }
    }
    abortRef.current?.abort();
    setStreaming(false);
  }, []);

  const clear = useCallback(() => {
    setInflight(null);
    setError(null);
  }, []);

  return { inflight, streaming, error, start, cancel, clear };
}

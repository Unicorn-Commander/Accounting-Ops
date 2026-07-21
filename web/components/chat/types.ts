// Shared types for the in-app /chat surface. Mirrors web/lib/chat.ts but
// adds the in-flight streaming shape the client uses while the SSE
// connection is open (e.g. partial text + tool call sequence as they
// arrive frame-by-frame).

import type {
  ChatMessage as ServerChatMessage,
  ChatPersona,
  ChatSessionHeader,
  ChatSessionSummary,
  ChatToolCall as ServerChatToolCall,
  ChatToolResult as ServerChatToolResult
} from "@/lib/chat";

export type { ChatPersona, ChatSessionHeader, ChatSessionSummary };

// A tool call/result pair as rendered in the conversation. The result
// may arrive after the call (streaming), or both at once when re-loading
// history.
export type ToolEvent = {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result?: ServerChatToolResult["result"];
  artifactKind?: ServerChatToolResult["artifact_kind"];
  artifactSize?: number;
};

// A rendered message in the thread. The discriminator is `role`, but
// assistant messages carry both `text` and optional `toolEvents` so the
// UI can interleave tool-call cards with the streamed text.
export type RenderedMessage =
  | {
      kind: "user";
      id: string;
      text: string;
      createdAt: string;
    }
  | {
      kind: "assistant";
      id: string;
      text: string;
      toolEvents: ToolEvent[];
      streaming: boolean;
      stopReason?: string | null;
      cancelled?: boolean;
      model?: string | null;
      createdAt?: string;
    };

export function fromServerMessage(m: ServerChatMessage): RenderedMessage | null {
  if (m.role === "user") {
    const text =
      typeof m.content === "object" && "text" in m.content
        ? (m.content as { text?: string }).text ?? ""
        : "";
    return {
      kind: "user",
      id: m.id,
      text,
      createdAt: m.created_at
    };
  }
  if (m.role === "assistant") {
    const content = m.content;
    let text = "";
    let toolCalls: ServerChatToolCall[] = [];
    let toolResults: ServerChatToolResult[] = [];
    let stopReason: string | null | undefined;
    let cancelled = false;
    if (typeof content === "object" && content !== null) {
      const c = content as {
        text?: string;
        tool_calls?: ServerChatToolCall[];
        tool_results?: ServerChatToolResult[];
        stop_reason?: string | null;
        cancelled?: boolean;
      };
      text = c.text ?? "";
      toolCalls = c.tool_calls ?? [];
      toolResults = c.tool_results ?? [];
      stopReason = c.stop_reason;
      cancelled = !!c.cancelled;
    }
    const toolEvents: ToolEvent[] = toolCalls.map((tc) => {
      const tr = toolResults.find((r) => r.id === tc.id);
      return {
        id: tc.id,
        name: tc.name,
        args: tc.args,
        result: tr?.result,
        artifactKind: tr?.artifact_kind ?? null,
        artifactSize: tr?.artifact_size ?? 0
      };
    });
    return {
      kind: "assistant",
      id: m.id,
      text,
      toolEvents,
      streaming: false,
      stopReason,
      cancelled,
      model: m.model,
      createdAt: m.created_at
    };
  }
  return null;
}

// SSE event shapes returned by the FastAPI runner.
export type StreamEvent =
  | {
      type: "session";
      session_id: string;
      user_message_id: string;
      assistant_message_id: string;
      persona: string;
      model: string;
    }
  | { type: "text_delta"; text: string }
  | { type: "tool_call"; id: string; name: string; args: Record<string, unknown> }
  | {
      type: "tool_result";
      id: string;
      name: string;
      result: ServerChatToolResult["result"];
      artifact_kind: ServerChatToolResult["artifact_kind"];
      artifact_size: number;
    }
  | {
      type: "stop";
      stop_reason: string;
      input_tokens?: number;
      output_tokens?: number;
      duration_ms?: number;
      tool_call_count?: number;
      model?: string;
      backend?: string;
    }
  | {
      type: "error";
      message: string;
      // Forward-compat: a future mid-stream credit gate may tag the error with a
      // machine code + the same fields as the pre-stream 402 body. The v1 UX does
      // NOT depend on these — per-workspace entitlement means the top-level
      // pre-stream check already covers a CFO→specialist sub-call.
      code?: string;
      upgrade_url?: string | null;
      byok_available?: boolean;
    };

// An artifact tracked by the canvas pane (open / preview).
export type CanvasArtifact = {
  // Stable key in case the user clicks several tools.
  key: string;
  toolName: string;
  artifactKind: "markdown" | "json" | "text";
  artifactSize: number;
  text: string;
};

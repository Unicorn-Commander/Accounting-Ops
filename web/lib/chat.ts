// Server-side fetchers for the in-app /chat surface.
//
// Mirrors lib/tax-review-queue.ts: no-store fetch to the FastAPI backend
// using the user's Keycloak access token. Backend enforces entity-scope +
// per-tool RBAC; the web layer is a thin proxy here for SSR / page data,
// and the browser hits the chat proxy at /api/chat/* for live messaging.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// ---------------------------------------------------------------------------
// Types — mirror app/api/chat.py
// ---------------------------------------------------------------------------

export type ChatPersona = {
  slug: string;
  display_name: string;
  one_line: string;
};

export type ChatPersonasResponse = {
  default: string;
  personas: ChatPersona[];
};

export type ChatSessionSummary = {
  id: string;
  entity_id: string;
  entity_display_name: string | null;
  entity_slug: string | null;
  title: string | null;
  agent_persona: string | null;
  tax_year: number | null;
  started_at: string;
  last_activity_at: string | null;
};

export type ChatSessionHeader = ChatSessionSummary & {
  workspace_id: string;
  agent_id: string;
  user_uc_uid: string;
  ended_at: string | null;
  cancelled_at: string | null;
};

export type ChatToolCall = {
  id: string;
  name: string;
  args: Record<string, unknown>;
};

export type ChatToolResult = {
  id: string;
  name: string;
  artifact_kind: "markdown" | "json" | "text" | null;
  artifact_size: number;
  // The full payload (an MCP result shape: { content: [...] })
  result: { content?: Array<{ type?: string; text?: string }> | unknown[] } & Record<
    string,
    unknown
  >;
};

export type ChatMessageContent =
  | { type: "text"; text: string }
  | {
      type: "mixed";
      text: string;
      tool_calls?: ChatToolCall[];
      tool_results?: ChatToolResult[];
      stop_reason?: string | null;
      cancelled?: boolean;
    };

export type ChatMessage = {
  id: string;
  session_id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: ChatMessageContent;
  submitter_uc_uid: string | null;
  model: string | null;
  backend: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  duration_ms: number | null;
  created_at: string;
};

export type ChatSessionDetail = {
  session: ChatSessionHeader;
  messages: ChatMessage[];
};

export type ChatSessionsResponse = {
  sessions: ChatSessionSummary[];
};

// ---------------------------------------------------------------------------
// Server-side fetchers
// ---------------------------------------------------------------------------

async function fetchBackend<T>(path: string, init?: RequestInit): Promise<T | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  try {
    const res = await fetch(`${INTERNAL_BASE}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        Accept: "application/json",
        "Content-Type": "application/json",
        ...(init?.headers ?? {})
      },
      cache: "no-store"
    });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export async function fetchChatPersonas(): Promise<ChatPersonasResponse | null> {
  return fetchBackend<ChatPersonasResponse>("/api/chat/personas");
}

export async function fetchChatSessions(
  workspaceId: string,
  opts: { entityId?: string; limit?: number } = {}
): Promise<ChatSessionSummary[]> {
  const params = new URLSearchParams();
  if (opts.entityId) params.set("entity_id", opts.entityId);
  if (opts.limit) params.set("limit", String(opts.limit));
  const qs = params.toString();
  const path = `/api/workspaces/${workspaceId}/chat/sessions${qs ? "?" + qs : ""}`;
  const res = await fetchBackend<ChatSessionsResponse>(path);
  return res?.sessions ?? [];
}

export async function fetchChatSession(
  workspaceId: string,
  sessionId: string
): Promise<ChatSessionDetail | null> {
  return fetchBackend<ChatSessionDetail>(
    `/api/workspaces/${workspaceId}/chat/sessions/${sessionId}`
  );
}

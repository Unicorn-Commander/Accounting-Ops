// Server-side and client-side utilities for the CPA Narrative endpoints.
//
// GET  /api/workspaces/{wid}/entities/{eid}/tax-narrative/{year}
//      Returns the latest narrative as text/markdown. 404 if none.
//
// POST /api/workspaces/{wid}/entities/{eid}/tax-narrative/{year}
//      Triggers narrative generation. Returns 202 + {run_id, poll_url, ...}.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type NarrativeStatus =
  | { state: "exists"; markdown: string; itemId: string | null; createdAt: string | null }
  | { state: "not_found" }
  | { state: "error"; message: string };

export type TriggerNarrativeResponse = {
  status: "started" | "exists";
  run_id?: string;
  message: string;
  item_id?: string;
  created_at?: string;
  poll_url?: string;
};

// ---------------------------------------------------------------------------
// Server-side: fetch the narrative markdown
// ---------------------------------------------------------------------------

export async function fetchNarrative(
  workspaceId: string,
  entityId: string,
  taxYear: number
): Promise<NarrativeStatus> {
  const session = await auth();
  if (!session?.accessToken) {
    return { state: "not_found" };
  }

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/entities/${entityId}/tax-narrative/${taxYear}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "text/markdown, text/plain, */*",
        },
        cache: "no-store",
      }
    );

    if (res.status === 404) {
      return { state: "not_found" };
    }

    if (!res.ok) {
      const text = await res.text().catch(() => `HTTP ${res.status}`);
      return { state: "error", message: text.slice(0, 300) };
    }

    const markdown = await res.text();
    const itemId = res.headers.get("X-Narrative-Item-Id");
    const createdAt = res.headers.get("X-Narrative-Created-At");

    return { state: "exists", markdown, itemId, createdAt };
  } catch (err) {
    return {
      state: "error",
      message: err instanceof Error ? err.message : "Network error",
    };
  }
}

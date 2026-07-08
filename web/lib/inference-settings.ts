// Server-side helper for /settings/inference (BYOK inference key status).
//
// Reads the workspace's BYOK status from the FastAPI backend with the
// signed-in user's Keycloak bearer. The full key is NEVER returned by the
// backend — only configured/provider/last-4. Mutations (save/remove) go
// through the browser-side catch-all proxy in
// web/app/api/workspaces/[...slug]/route.ts.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type ByokProvider = "anthropic" | "openrouter";

export type ByokStatus = {
  configured: boolean;
  provider: string | null;
  key_last4: string | null;
  base_url: string | null;
};

export async function getByokStatus(
  workspaceId: string
): Promise<ByokStatus | null> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return null;
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${encodeURIComponent(
        workspaceId
      )}/inference/byok`,
      {
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/json",
        },
        cache: "no-store",
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as ByokStatus;
  } catch {
    return null;
  }
}

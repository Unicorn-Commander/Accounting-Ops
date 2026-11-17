// Client-safe classification of a failed backend call, shared by the Stripe
// review actions and the blocked-item panel. No server imports: this module is
// bundled into client components and imported by the tsx smoke tests.

export type ApiFailureKind = "human_session" | "retry" | "forbidden" | "error";

export type ApiFailure = { ok: false; kind: ApiFailureKind; status: number; message: string };

export const HUMAN_SESSION_MESSAGE =
  "This decision needs you signed in yourself in the browser. Agents, service tokens and personal access tokens cannot make it. Sign in again and retry.";

export const RETRY_MESSAGE =
  "A Stripe sync pass is running right now. Nothing was changed. Wait a few seconds and try again.";

/** The FastAPI `detail` (string or validation list) as one readable line. */
export function detailText(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => (d && typeof d === "object" && typeof (d as { msg?: unknown }).msg === "string" ? (d as { msg: string }).msg : null))
      .filter((m): m is string => Boolean(m));
    if (parts.length) return parts.join("; ");
  }
  const error = (body as { error?: unknown }).error;
  return typeof error === "string" && error.trim() ? error.trim() : null;
}

export function classifyFailure(status: number, body: unknown): ApiFailure {
  const detail = detailText(body);
  if (status === 403 && detail && /human session/i.test(detail)) {
    return { ok: false, kind: "human_session", status, message: HUMAN_SESSION_MESSAGE };
  }
  if (status === 409 && detail && /retry shortly/i.test(detail)) {
    return { ok: false, kind: "retry", status, message: RETRY_MESSAGE };
  }
  if (status === 403) {
    return { ok: false, kind: "forbidden", status, message: detail ?? "You do not have permission to do this." };
  }
  return { ok: false, kind: "error", status, message: detail ?? `Request failed (HTTP ${status}).` };
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/** POST JSON and return the parsed body, or a classified failure. */
export async function postJson<T>(
  fetchImpl: FetchLike,
  url: string,
  body: unknown,
): Promise<{ ok: true; data: T } | ApiFailure> {
  let res: Response;
  try {
    res = await fetchImpl(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    return { ok: false, kind: "error", status: 0, message: "Could not reach the server. Check your connection and retry." };
  }
  const parsed: unknown = await res.json().catch(() => ({}));
  if (!res.ok) return classifyFailure(res.status, parsed);
  return { ok: true, data: parsed as T };
}

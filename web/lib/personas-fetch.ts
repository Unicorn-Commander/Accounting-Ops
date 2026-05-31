// Server-side fetch for /api/chat/personas. Falls back to the static list in
// personas.ts when the app container isn't reachable (so the page never
// 500s on a fresh deploy).

import { auth } from "@/lib/auth";
import {
  FALLBACK_PERSONAS,
  Persona,
  PersonasResponse
} from "@/lib/personas";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export interface PersonasResult {
  personas: Persona[];
  default: string;
  brigade_online: boolean;
  brigade_reason: string;
  /** true when the result is a fallback because /api/chat/personas was
   *  unreachable (network error / 401 / 500). */
  is_fallback: boolean;
}

export async function fetchPersonas(): Promise<PersonasResult> {
  const fallback: PersonasResult = {
    personas: FALLBACK_PERSONAS,
    default: "cpa-tax-resolver",
    brigade_online: false,
    brigade_reason: "no-session",
    is_fallback: true
  };

  let token: string | undefined;
  try {
    const session = await auth();
    token = session?.accessToken;
  } catch {
    return fallback;
  }
  if (!token) return fallback;

  try {
    const res = await fetch(`${INTERNAL_BASE}/api/chat/personas`, {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json"
      },
      cache: "no-store"
    });
    if (!res.ok) {
      return {
        ...fallback,
        brigade_reason: `fetch_failed: ${res.status}`
      };
    }
    const body = (await res.json()) as PersonasResponse;
    return {
      personas: body.personas ?? FALLBACK_PERSONAS,
      default: body.default ?? "cpa-tax-resolver",
      brigade_online: !!body.brigade_online,
      brigade_reason: body.brigade_reason ?? "unknown",
      is_fallback: false
    };
  } catch (exc) {
    return {
      ...fallback,
      brigade_reason: `fetch_error: ${(exc as Error).message ?? "unknown"}`
    };
  }
}

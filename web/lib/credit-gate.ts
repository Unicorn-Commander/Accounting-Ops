// Shared plumbing for the "this feature needs credits" (HTTP 402) UX.
//
// Paid "smart" AI calls — the CFO/Opus advisor and the tax specialists —
// return HTTP 402 when a workspace has neither an Accounting-Ops entitlement
// nor a BYOK key. Free deterministic bookkeeping is NEVER gated. The 402 body
// is a frozen contract:
//   { detail: string, upgrade_url: string | null, byok_available: boolean }
//
// `parsePaymentRequired` normalizes that body into a CreditGate (or null when
// the response isn't a 402). `openCreditGate` broadcasts a gate to the
// once-mounted <CreditGateModal>. The event bus is cloned from
// components/global-toast.tsx; unlike the toast it does NOT persist to
// sessionStorage — a 402 fires inside a live fetch with no navigation, so
// there's no SSR round trip to survive (and a stale gate must not resurface on
// the next page load).

export type CreditGate = {
  detail: string;
  upgradeUrl: string | null;
  byokAvailable: boolean;
};

const DEFAULT_DETAIL =
  "This feature runs on paid AI. Add credits or bring your own API key to continue.";

/**
 * Parse a fetch Response into a CreditGate. Returns null unless the response is
 * a 402 Payment Required. The body is read from a clone (so the caller can
 * still consume the original) and parsed defensively — a malformed, empty, or
 * non-JSON body still yields a usable gate, because BYOK is always an escape
 * hatch even when the endpoint isn't fully deployed yet.
 */
export async function parsePaymentRequired(
  res: Response
): Promise<CreditGate | null> {
  if (res.status !== 402) return null;
  let body: unknown = null;
  try {
    body = await res.clone().json();
  } catch {
    // Non-JSON / empty body — fall back to the defaults below.
  }
  const b = (body ?? {}) as {
    detail?: unknown;
    upgrade_url?: unknown;
    byok_available?: unknown;
  };
  return {
    detail:
      typeof b.detail === "string" && b.detail.trim() ? b.detail : DEFAULT_DETAIL,
    upgradeUrl: typeof b.upgrade_url === "string" ? b.upgrade_url : null,
    // BYOK is offered unless the backend explicitly opts out.
    byokAvailable: b.byok_available !== false,
  };
}

// ---------------------------------------------------------------------------
// Event bus — show the credit-gate modal from anywhere (cloned from
// components/global-toast.tsx). A paid call that 402s calls openCreditGate(gate);
// the once-mounted <CreditGateModal> subscribes and opens.
// ---------------------------------------------------------------------------

const CREDIT_GATE_EVENT = "aops:credit-gate";

export function openCreditGate(gate: CreditGate): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<CreditGate>(CREDIT_GATE_EVENT, { detail: gate })
  );
}

export function subscribeCreditGate(
  handler: (gate: CreditGate) => void
): () => void {
  if (typeof window === "undefined") return () => {};
  const listener = (e: Event) => {
    const gate = (e as CustomEvent<CreditGate>).detail;
    if (gate) handler(gate);
  };
  window.addEventListener(CREDIT_GATE_EVENT, listener);
  return () => window.removeEventListener(CREDIT_GATE_EVENT, listener);
}

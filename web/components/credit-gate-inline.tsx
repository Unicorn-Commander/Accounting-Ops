// Community Edition: inert stub (no credit gating). Enterprise supplies the
// real inline gate. Rendered only when a gate is active, which never happens
// in CE, so this returns nothing.
import type { CreditGate } from "@/lib/credit-gate";

export function CreditGateInline(_props: { gate: CreditGate }) {
  return null;
}

// Community Edition: no credit gating. These are inert stubs so the shared
// chat/shell components compile unchanged; Enterprise supplies the real
// implementations. The gate path below is dead code in CE (parsePaymentRequired
// always returns null, so openCreditGate is never reached).
export type CreditGate = {
  detail: string | null;
  message?: string;
  balanceCents?: number;
  requiredCents?: number;
};

export async function parsePaymentRequired(_res: unknown): Promise<CreditGate | null> {
  // CE never returns a 402/payment-required gate.
  return null;
}

export function openCreditGate(_gate: CreditGate): void {
  /* no-op in Community Edition */
}

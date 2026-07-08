"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { Ban, CheckCircle2, Loader2 } from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import { MoneyParseError, formatCents, parseDollars } from "@/lib/money";
import type { InvoiceStatus } from "@/lib/invoices";

export function InvoiceActions({
  workspaceId,
  invoiceId,
  status,
  balanceDue,
  currency,
  hasPayments
}: {
  workspaceId: string;
  invoiceId: string;
  status: InvoiceStatus;
  balanceDue: string;
  currency: string;
  hasPayments: boolean;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [payAmount, setPayAmount] = useState(balanceDue);
  const [payMethod, setPayMethod] = useState("manual");
  const [voiding, setVoiding] = useState(false);
  const [voidReason, setVoidReason] = useState("");

  const isClosed = status === "void" || status === "paid";
  const balanceNum = parseFloat(balanceDue);

  async function post(path: string, body: unknown) {
    const res = await fetch(
      `/api/workspaces/${workspaceId}/invoices/${invoiceId}/${path}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      }
    );
    if (!res.ok) {
      const b = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
      throw new Error(b.detail ?? "Request failed");
    }
    return res.json();
  }

  function markPaid() {
    setError(null);
    let cents: number;
    try {
      cents = parseDollars(payAmount);
    } catch (e) {
      setError(e instanceof MoneyParseError ? e.message : "Invalid amount");
      return;
    }
    if (cents <= 0) return setError("Amount must be positive.");
    startTransition(async () => {
      try {
        await post("payments", {
          amount: formatCents(cents),
          method: payMethod
        });
        router.refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Payment failed");
      }
    });
  }

  function voidInvoice() {
    setError(null);
    if (!voidReason.trim()) return setError("A void reason is required.");
    startTransition(async () => {
      try {
        await post("void", { reason: voidReason.trim() });
        setVoiding(false);
        router.refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Void failed");
      }
    });
  }

  return (
    <Card>
      <p className="mb-3 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
        Actions
      </p>

      {!isClosed && balanceNum > 0 && (
        <div className="space-y-2">
          <label className="block text-[10px] uppercase tracking-wider text-ink-tertiary">
            Record payment
          </label>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={payAmount}
              onChange={(e) => setPayAmount(e.target.value)}
              aria-label="Payment amount"
              className="w-28 rounded-md border border-default bg-canvas py-1.5 px-2 text-sm font-mono"
            />
            <select
              value={payMethod}
              onChange={(e) => setPayMethod(e.target.value)}
              aria-label="Payment method"
              className="flex-1 rounded-md border border-default bg-canvas py-1.5 px-2 text-sm"
            >
              {["manual", "ach", "card", "check", "wire", "cash", "other"].map(
                (m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                )
              )}
            </select>
          </div>
          <button
            onClick={markPaid}
            disabled={pending}
            className={cn(
              "inline-flex w-full items-center justify-center gap-1.5 rounded-md bg-credit/90 px-3 py-2 text-sm font-medium text-white hover:bg-credit",
              pending && "cursor-wait opacity-60"
            )}
          >
            {pending ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <CheckCircle2 size={13} />
            )}
            Mark paid ({formatMoney(payAmount, currency)})
          </button>
        </div>
      )}

      {status === "paid" && (
        <p className="rounded-md border border-credit/30 bg-credit/[0.06] px-3 py-2 text-xs text-credit">
          Fully paid — balance is zero.
        </p>
      )}
      {status === "void" && (
        <p className="rounded-md border border-blocked/30 bg-blocked/[0.06] px-3 py-2 text-xs text-blocked">
          This invoice is void (reversed in the ledger).
        </p>
      )}

      {/* Void (only when not already void/paid and no payments) */}
      {status !== "void" && !hasPayments && (
        <div className="mt-4 border-t border-subtle pt-4">
          {!voiding ? (
            <button
              onClick={() => setVoiding(true)}
              className="inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-debit/40 bg-transparent px-3 py-2 text-sm font-medium text-debit hover:bg-debit/[0.06]"
            >
              <Ban size={13} /> Void invoice
            </button>
          ) : (
            <div className="space-y-2">
              <input
                type="text"
                value={voidReason}
                onChange={(e) => setVoidReason(e.target.value)}
                placeholder="Reason (required)"
                aria-label="Void reason"
                className="w-full rounded-md border border-default bg-canvas py-1.5 px-2 text-sm"
              />
              <div className="flex gap-2">
                <button
                  onClick={voidInvoice}
                  disabled={pending}
                  className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md bg-debit/90 px-3 py-2 text-sm font-medium text-white hover:bg-debit"
                >
                  {pending ? <Loader2 size={13} className="animate-spin" /> : null}
                  Confirm void
                </button>
                <button
                  onClick={() => {
                    setVoiding(false);
                    setVoidReason("");
                  }}
                  className="rounded-md border border-default bg-surface px-3 py-2 text-sm text-ink-secondary hover:bg-elevated"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="mt-3 rounded-md border border-debit/40 bg-debit/[0.06] px-3 py-2 text-xs text-debit">
          {error}
        </div>
      )}
    </Card>
  );
}

function formatMoney(value: string, currency = "USD"): string {
  const n = parseFloat(value);
  if (Number.isNaN(n)) return "—";
  const sym = currency === "USD" ? "$" : `${currency} `;
  return `${sym}${Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })}`;
}

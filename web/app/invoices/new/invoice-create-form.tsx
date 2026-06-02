"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState, useTransition } from "react";
import { Loader2, Plus, Trash2 } from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import { MoneyParseError, formatCents, parseDollars } from "@/lib/money";

type Entity = { id: string; display_name: string };

type LineDraft = {
  description: string;
  quantity: string;
  unitPrice: string;
  taxAmount: string;
};

const EMPTY_LINE: LineDraft = {
  description: "",
  quantity: "1",
  unitPrice: "",
  taxAmount: ""
};

export function InvoiceCreateForm({
  workspaceId,
  entities,
  initialEntityId
}: {
  workspaceId: string;
  entities: Entity[];
  initialEntityId: string;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const [entityId, setEntityId] = useState(initialEntityId);
  const [customerName, setCustomerName] = useState("");
  const [customerEmail, setCustomerEmail] = useState("");
  const [billingAddress, setBillingAddress] = useState("");
  const [issueDate, setIssueDate] = useState(
    new Date().toISOString().slice(0, 10)
  );
  const [netTerms, setNetTerms] = useState("30");
  const [poNumber, setPoNumber] = useState("");
  const [notes, setNotes] = useState("");
  const [discount, setDiscount] = useState("");
  const [autoSend, setAutoSend] = useState(false);
  const [lines, setLines] = useState<LineDraft[]>([{ ...EMPTY_LINE }]);

  // Live totals (integer-cents math; never float-sums).
  const totals = useMemo(() => {
    let subtotalCents = 0;
    let taxCents = 0;
    let ok = true;
    for (const l of lines) {
      try {
        const qty = parseFloat(l.quantity || "0");
        const unitCents = l.unitPrice ? parseDollars(l.unitPrice) : 0;
        if (!Number.isFinite(qty)) ok = false;
        subtotalCents += Math.round(qty * unitCents);
        if (l.taxAmount) taxCents += parseDollars(l.taxAmount);
      } catch {
        ok = false;
      }
    }
    let discountCents = 0;
    try {
      discountCents = discount ? parseDollars(discount) : 0;
    } catch {
      ok = false;
    }
    const totalCents = subtotalCents - discountCents + taxCents;
    return { subtotalCents, taxCents, discountCents, totalCents, ok };
  }, [lines, discount]);

  function updateLine(idx: number, patch: Partial<LineDraft>) {
    setLines((prev) =>
      prev.map((l, i) => (i === idx ? { ...l, ...patch } : l))
    );
  }
  function addLine() {
    setLines((prev) => [...prev, { ...EMPTY_LINE }]);
  }
  function removeLine(idx: number) {
    setLines((prev) =>
      prev.length === 1 ? prev : prev.filter((_, i) => i !== idx)
    );
  }

  async function submit() {
    setError(null);
    if (!entityId) return setError("Pick a billing entity.");
    if (!customerName.trim()) return setError("Customer name is required.");

    const cleanedLines: {
      description: string;
      quantity: number;
      unit_price: string;
      tax_amount?: string;
    }[] = [];
    for (const [i, l] of lines.entries()) {
      if (!l.description.trim()) {
        return setError(`Line ${i + 1}: description is required.`);
      }
      let unitCents: number;
      try {
        unitCents = l.unitPrice ? parseDollars(l.unitPrice) : 0;
      } catch (e) {
        return setError(
          e instanceof MoneyParseError
            ? `Line ${i + 1}: ${e.message}`
            : `Line ${i + 1}: invalid unit price`
        );
      }
      const qty = parseFloat(l.quantity || "0");
      if (!Number.isFinite(qty) || qty <= 0) {
        return setError(`Line ${i + 1}: quantity must be a positive number.`);
      }
      let taxStr: string | undefined;
      if (l.taxAmount) {
        try {
          taxStr = formatCents(parseDollars(l.taxAmount));
        } catch {
          return setError(`Line ${i + 1}: invalid tax amount.`);
        }
      }
      cleanedLines.push({
        description: l.description.trim(),
        quantity: qty,
        unit_price: formatCents(unitCents),
        ...(taxStr ? { tax_amount: taxStr } : {})
      });
    }

    if (totals.totalCents <= 0) {
      return setError("Invoice total must be positive.");
    }

    const body = {
      entity_id: entityId,
      external_source: "manual",
      external_ref: null,
      customer: {
        name: customerName.trim(),
        email: customerEmail.trim() || null,
        billing_address: billingAddress.trim() || null
      },
      line_items: cleanedLines,
      currency: "USD",
      issue_date: issueDate,
      net_terms: netTerms ? parseInt(netTerms, 10) : 30,
      discount: discount ? formatCents(totals.discountCents) : undefined,
      notes: notes.trim() || null,
      po_number: poNumber.trim() || null,
      auto_send: autoSend
    };

    startTransition(async () => {
      try {
        const res = await fetch(`/api/workspaces/${workspaceId}/invoices`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        if (!res.ok) {
          const b = await res
            .json()
            .catch(() => ({ detail: `HTTP ${res.status}` }));
          throw new Error(b.detail ?? "Create failed");
        }
        const created = await res.json();
        router.push(`/invoices/${created.id}`);
        router.refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
      }
    });
  }

  return (
    <Card>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Field label="Billing entity">
          <select
            value={entityId}
            onChange={(e) => setEntityId(e.target.value)}
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
          >
            {entities.map((e) => (
              <option key={e.id} value={e.id}>
                {e.display_name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Customer name">
          <input
            type="text"
            value={customerName}
            onChange={(e) => setCustomerName(e.target.value)}
            placeholder="Acme LLC"
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
          />
        </Field>
        <Field label="Customer email" hint="Optional — shown on the invoice.">
          <input
            type="email"
            value={customerEmail}
            onChange={(e) => setCustomerEmail(e.target.value)}
            placeholder="ap@acme.com"
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
          />
        </Field>
        <Field label="Billing address" hint="Optional.">
          <input
            type="text"
            value={billingAddress}
            onChange={(e) => setBillingAddress(e.target.value)}
            placeholder="123 Main St, City, ST"
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
          />
        </Field>
        <Field label="Issue date">
          <input
            type="date"
            value={issueDate}
            onChange={(e) => setIssueDate(e.target.value)}
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
          />
        </Field>
        <Field label="Net terms (days)" hint="Due date = issue + N days.">
          <input
            type="number"
            min={0}
            value={netTerms}
            onChange={(e) => setNetTerms(e.target.value)}
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm font-mono"
          />
        </Field>
        <Field label="PO number" hint="Optional buyer reference.">
          <input
            type="text"
            value={poNumber}
            onChange={(e) => setPoNumber(e.target.value)}
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
          />
        </Field>
        <Field label="Discount (USD)" hint="Optional invoice-level discount.">
          <input
            type="text"
            value={discount}
            onChange={(e) => setDiscount(e.target.value)}
            placeholder="0.00"
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm font-mono"
          />
        </Field>
      </div>

      {/* Line items */}
      <div className="mt-6">
        <div className="mb-2 flex items-center justify-between">
          <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
            Line items
          </p>
          <button
            type="button"
            onClick={addLine}
            className="inline-flex items-center gap-1 rounded-md border border-default bg-surface px-2 py-1 text-[11px] text-ink-secondary hover:bg-elevated"
          >
            <Plus size={11} /> Add line
          </button>
        </div>
        <div className="space-y-2">
          {lines.map((l, idx) => (
            <div
              key={idx}
              className="grid grid-cols-12 items-center gap-2 rounded-md border border-subtle bg-canvas/40 p-2"
            >
              <input
                type="text"
                value={l.description}
                onChange={(e) => updateLine(idx, { description: e.target.value })}
                placeholder="Description"
                aria-label={`Line ${idx + 1} description`}
                className="col-span-5 rounded-md border border-default bg-canvas py-1.5 px-2 text-sm"
              />
              <input
                type="number"
                step="0.01"
                value={l.quantity}
                onChange={(e) => updateLine(idx, { quantity: e.target.value })}
                placeholder="Qty"
                aria-label={`Line ${idx + 1} quantity`}
                className="col-span-2 rounded-md border border-default bg-canvas py-1.5 px-2 text-sm font-mono"
              />
              <input
                type="text"
                value={l.unitPrice}
                onChange={(e) => updateLine(idx, { unitPrice: e.target.value })}
                placeholder="Unit $"
                aria-label={`Line ${idx + 1} unit price`}
                className="col-span-2 rounded-md border border-default bg-canvas py-1.5 px-2 text-sm font-mono"
              />
              <input
                type="text"
                value={l.taxAmount}
                onChange={(e) => updateLine(idx, { taxAmount: e.target.value })}
                placeholder="Tax $"
                aria-label={`Line ${idx + 1} tax`}
                className="col-span-2 rounded-md border border-default bg-canvas py-1.5 px-2 text-sm font-mono"
              />
              <button
                type="button"
                onClick={() => removeLine(idx)}
                disabled={lines.length === 1}
                aria-label={`Remove line ${idx + 1}`}
                className="col-span-1 flex items-center justify-center text-ink-tertiary hover:text-debit disabled:opacity-30"
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      </div>

      <Field label="Notes" className="mt-4">
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={2}
          placeholder="Payment instructions, thank-you note, etc."
          className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
        />
      </Field>

      {/* Totals + submit */}
      <div className="mt-5 flex flex-col items-end gap-1 border-t border-subtle pt-4 text-sm">
        <Totals label="Subtotal" cents={totals.subtotalCents} />
        {totals.discountCents > 0 && (
          <Totals label="Discount" cents={-totals.discountCents} />
        )}
        {totals.taxCents > 0 && <Totals label="Sales tax" cents={totals.taxCents} />}
        <Totals label="Total" cents={totals.totalCents} bold />
      </div>

      <label className="mt-4 flex items-center justify-end gap-2 text-xs text-ink-secondary">
        <input
          type="checkbox"
          checked={autoSend}
          onChange={(e) => setAutoSend(e.target.checked)}
        />
        Mark as sent immediately (else saved as draft)
      </label>

      {error && (
        <div className="mt-4 rounded-md border border-debit/40 bg-debit/[0.06] px-3 py-2 text-sm text-debit">
          {error}
        </div>
      )}

      <div className="mt-4 flex items-center justify-end">
        <button
          onClick={submit}
          disabled={pending || !totals.ok}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white hover:bg-brand-600",
            (pending || !totals.ok) && "cursor-not-allowed opacity-60"
          )}
        >
          {pending ? <Loader2 size={13} className="animate-spin" /> : null}
          {pending ? "Creating…" : "Create invoice"}
        </button>
      </div>
    </Card>
  );
}

function Totals({
  label,
  cents,
  bold
}: {
  label: string;
  cents: number;
  bold?: boolean;
}) {
  return (
    <div className="flex w-56 items-center justify-between">
      <span
        className={cn(
          "text-ink-tertiary",
          bold && "font-semibold text-ink-primary"
        )}
      >
        {label}
      </span>
      <span
        className={cn(
          "font-mono tabular-nums text-ink-secondary",
          bold && "font-semibold text-ink-primary"
        )}
      >
        ${formatCents(cents)}
      </span>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
  className
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      <label className="mb-1 block text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
        {label}
      </label>
      {children}
      {hint && <p className="mt-1 text-[10px] text-ink-tertiary">{hint}</p>}
    </div>
  );
}

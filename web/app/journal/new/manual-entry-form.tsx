"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, useTransition } from "react";
import { Loader2 } from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import { MoneyParseError, parseDollars } from "@/lib/money";

type Entity = { id: string; display_name: string };
type CoaAccount = {
  account_number: string;
  account_name: string;
  canonical_form: string;
  account_type: string;
  normal_balance: string;
};

const CATEGORY_OPTIONS = [
  "Revenue · Marketplace",
  "Revenue · Music",
  "Revenue · Services",
  "Revenue · Other",
  "SaaS · AI",
  "SaaS · Email",
  "SaaS · Productivity",
  "Infrastructure · CDN",
  "Infrastructure · VPS",
  "Infrastructure · Network",
  "Banking · Fees",
  "Banking · Interest",
  "Inter-entity transfer",
  "Travel · Lodging",
  "Travel · Transport",
  "Meals",
  "Office · Supplies",
  "Contractor · 1099-NEC",
  "Owner Contribution",
  "Owner Distribution",
  "Other Income",
  "Other Expense"
];

export function ManualEntryForm({
  workspaceId,
  entities,
  initialEntityId,
  initialCoa
}: {
  workspaceId: string;
  entities: Entity[];
  initialEntityId: string;
  initialCoa: CoaAccount[];
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [posted, setPosted] = useState<{ id: string; tier: string } | null>(null);

  const [entityId, setEntityId] = useState(initialEntityId);
  const [coa, setCoa] = useState<CoaAccount[]>(initialCoa);
  const [effectiveDate, setEffectiveDate] = useState(
    new Date().toISOString().slice(0, 10)
  );
  const [amount, setAmount] = useState("");
  const [debitAccount, setDebitAccount] = useState("");
  const [creditAccount, setCreditAccount] = useState("");
  const [description, setDescription] = useState("");
  const [payee, setPayee] = useState("");
  const [memo, setMemo] = useState("");
  const [category, setCategory] = useState("");
  const [sourceExternalId, setSourceExternalId] = useState("");

  useEffect(() => {
    if (!entityId) return;
    let cancelled = false;
    fetch(`/api/coa/${workspaceId}/${entityId}`)
      .then((r) => (r.ok ? r.json() : { accounts: [] }))
      .then((body) => {
        if (!cancelled) setCoa(body.accounts ?? []);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [entityId, workspaceId]);

  async function submit() {
    setError(null);
    setPosted(null);
    // Audit 2026-05-31 Bug 14: parse via integer-cents helper instead of
    // parseFloat + Math.round; rejects more-than-2-decimal-place inputs cleanly.
    let parsedCents: number;
    try {
      parsedCents = parseDollars(amount);
    } catch (e) {
      setError(
        e instanceof MoneyParseError
          ? e.message
          : "Amount must be a valid dollar amount"
      );
      return;
    }
    if (parsedCents === 0) {
      setError("Amount must be a non-zero number");
      return;
    }
    if (!debitAccount || !creditAccount) {
      setError("Both debit and credit accounts are required");
      return;
    }
    if (debitAccount === creditAccount) {
      setError("Debit and credit cannot be the same account");
      return;
    }
    if (!category) {
      setError("Pick a category");
      return;
    }

    const amountCents = Math.abs(parsedCents);
    // Determine sign convention: if debit is asset and credit is revenue, this
    // is an inflow (positive). If debit is expense and credit is asset, this
    // is an outflow (negative). We pass the signed amount_cents the backend
    // expects.
    const debitAcct = coa.find((a) => a.canonical_form === debitAccount);
    const creditAcct = coa.find((a) => a.canonical_form === creditAccount);
    const debitIsExpense =
      debitAcct?.account_type === "expense" ||
      debitAcct?.account_type === "cogs" ||
      debitAcct?.account_type === "other_expense" ||
      debitAcct?.account_type === "tax";
    const signedAmountCents = debitIsExpense ? -amountCents : amountCents;

    startTransition(async () => {
      try {
        // Manual entries use the same /journal-entries endpoint with
        // source="manual". We use a synthesized source_external_id so
        // re-submits are idempotent.
        const ext = sourceExternalId.trim() || `manual-${Date.now()}`;
        const res = await fetch(`/api/workspaces/${workspaceId}/journal-entries`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            entity_id: entityId,
            source: "manual",
            source_external_id: ext,
            effective_date: effectiveDate,
            amount_cents: signedAmountCents,
            description: description || payee || "Manual entry",
            payee,
            memo,
            category,
            category_status: "auto",
            sign_matched: true,
            external_account_id: null,
            user_confirmed: true,
            reasoning_trace: "Manual entry via /journal/new",
            model_version: "manual"
          })
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
          throw new Error(body.detail ?? "Post failed");
        }
        const body = await res.json();
        if (body.ok) {
          setPosted({
            id: body.journal_entry_id,
            tier: body.materiality_tier
          });
          // Reset form
          setAmount("");
          setDescription("");
          setPayee("");
          setMemo("");
          setSourceExternalId("");
          router.refresh();
        } else {
          setError(body.reason ?? body.outcome);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
      }
    });
  }

  return (
    <Card>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Field id="manual-entity" label="Entity">
          <select
            id="manual-entity"
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
        <Field id="manual-effective-date" label="Effective date">
          <input
            id="manual-effective-date"
            type="date"
            value={effectiveDate}
            onChange={(e) => setEffectiveDate(e.target.value)}
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
          />
        </Field>

        <Field
          id="manual-amount"
          label="Amount (USD)"
          hint="Positive number; direction is determined by accounts."
        >
          <input
            id="manual-amount"
            type="number"
            step="0.01"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            placeholder="0.00"
            aria-describedby="manual-amount-hint"
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm font-mono"
          />
        </Field>
        <Field id="manual-category" label="Category">
          <select
            id="manual-category"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
          >
            <option value="">— pick category —</option>
            {CATEGORY_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </Field>

        <Field
          id="manual-debit-account"
          label="Debit account"
          hint="Where the value goes IN (expense/asset on outflow; asset on inflow)."
        >
          <AccountSelect
            id="manual-debit-account"
            coa={coa}
            value={debitAccount}
            onChange={setDebitAccount}
            describedBy="manual-debit-account-hint"
          />
        </Field>
        <Field
          id="manual-credit-account"
          label="Credit account"
          hint="Where the value comes FROM (cash on outflow; revenue/owner on inflow)."
        >
          <AccountSelect
            id="manual-credit-account"
            coa={coa}
            value={creditAccount}
            onChange={setCreditAccount}
            describedBy="manual-credit-account-hint"
          />
        </Field>

        <Field id="manual-payee" label="Payee">
          <input
            id="manual-payee"
            type="text"
            value={payee}
            onChange={(e) => setPayee(e.target.value)}
            placeholder="e.g. Aaron Stransky"
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
          />
        </Field>
        <Field
          id="manual-source-id"
          label="Source ID"
          hint="Optional — for idempotency. Defaults to manual-{timestamp}."
        >
          <input
            id="manual-source-id"
            type="text"
            value={sourceExternalId}
            onChange={(e) => setSourceExternalId(e.target.value)}
            placeholder="manual-2026-05-22-owner-contrib"
            aria-describedby="manual-source-id-hint"
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm font-mono"
          />
        </Field>

        <Field id="manual-description" label="Description" className="md:col-span-2">
          <input
            id="manual-description"
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="e.g. Owner contribution — Q2 cash injection"
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
          />
        </Field>
        <Field id="manual-memo" label="Memo" className="md:col-span-2">
          <textarea
            id="manual-memo"
            value={memo}
            onChange={(e) => setMemo(e.target.value)}
            rows={2}
            className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm"
          />
        </Field>
      </div>

      {error && (
        <div className="mt-4 rounded-md border border-debit/40 bg-debit/[0.06] px-3 py-2 text-sm text-debit">
          {error}
        </div>
      )}
      {posted && (
        <div className="mt-4 rounded-md border border-credit/40 bg-credit/[0.06] px-3 py-2 text-sm text-credit">
          Posted entry <span className="font-mono">{posted.id.slice(0, 8)}</span> as tier {posted.tier}.
        </div>
      )}

      <div className="mt-4 flex items-center justify-end gap-2">
        <button
          onClick={submit}
          disabled={pending}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white hover:bg-brand-600",
            pending && "cursor-wait opacity-60"
          )}
        >
          {pending ? <Loader2 size={13} className="animate-spin" /> : null}
          {pending ? "Posting…" : "Post entry"}
        </button>
      </div>
    </Card>
  );
}

function Field({
  id,
  label,
  hint,
  children,
  className
}: {
  id: string;
  label: string;
  hint?: string;
  children: React.ReactNode;
  className?: string;
}) {
  const hintId = hint ? `${id}-hint` : undefined;
  return (
    <div className={className}>
      <label
        htmlFor={id}
        className="mb-1 block text-[10px] font-medium uppercase tracking-wider text-ink-tertiary"
      >
        {label}
      </label>
      {children}
      {hint && (
        <p id={hintId} className="mt-1 text-[10px] text-ink-tertiary">
          {hint}
        </p>
      )}
    </div>
  );
}

function AccountSelect({
  id,
  coa,
  value,
  onChange,
  describedBy
}: {
  id?: string;
  coa: CoaAccount[];
  value: string;
  onChange: (v: string) => void;
  describedBy?: string;
}) {
  const groups: Record<string, CoaAccount[]> = {};
  for (const a of coa) {
    const k = a.account_type;
    (groups[k] ??= []).push(a);
  }
  const order = [
    "asset",
    "liability",
    "equity",
    "revenue",
    "cogs",
    "expense",
    "other_income",
    "other_expense",
    "tax"
  ];
  return (
    <select
      id={id}
      aria-describedby={describedBy}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full rounded-md border border-default bg-canvas py-1.5 px-3 text-sm font-mono"
    >
      <option value="">— pick account —</option>
      {order
        .filter((k) => groups[k])
        .map((k) => (
          <optgroup key={k} label={k.toUpperCase()}>
            {groups[k]
              .sort((a, b) => a.account_number.localeCompare(b.account_number))
              .map((a) => (
                <option key={a.canonical_form} value={a.canonical_form}>
                  {a.canonical_form}
                </option>
              ))}
          </optgroup>
        ))}
    </select>
  );
}

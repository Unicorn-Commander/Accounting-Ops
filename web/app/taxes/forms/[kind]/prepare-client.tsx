"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, useTransition } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  FileText,
  Info,
  Loader2,
  Plus,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { Card, ErrorState } from "@/components/primitives";
import { cn } from "@/lib/cn";
import * as forms from "@/lib/forms";
import type {
  Compute1040Input,
  ComputeW4pInput,
  FieldInput,
  FormFieldMeta,
  FormSpecDetail,
} from "@/lib/forms";

// ---------------------------------------------------------------------------
// Shared input chrome — matches the codebase convention used across forms
// (members-client.tsx etc.): a labeled control on the dark theme.
// ---------------------------------------------------------------------------

const INPUT_CLS =
  "mt-1 block w-full rounded-md border border-default bg-elevated px-3 py-2 text-sm text-ink-primary placeholder:text-ink-disabled focus:outline-none focus:ring-2 focus:ring-brand-500/40";

function Field({
  label,
  hint,
  tag,
  htmlFor,
  children,
}: {
  label: string;
  hint?: string;
  tag?: string;
  htmlFor?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label
        htmlFor={htmlFor}
        className="flex items-center gap-2 text-xs font-medium text-ink-secondary"
      >
        <span>{label}</span>
        {tag ? (
          <span className="rounded-sm bg-white/[0.06] px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-ink-tertiary">
            {tag}
          </span>
        ) : null}
      </label>
      {children}
      {hint ? (
        <p className="mt-1 text-[10px] leading-relaxed text-ink-tertiary">{hint}</p>
      ) : null}
    </div>
  );
}

/** A currency input with a "$" adornment. Stores the raw string in state. */
function CurrencyInput({
  id,
  value,
  onChange,
  placeholder = "0.00",
}: {
  id?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div className="relative mt-1">
      <span className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 text-sm text-ink-tertiary">
        $
      </span>
      <input
        id={id}
        type="text"
        inputMode="decimal"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={cn(INPUT_CLS, "mt-0 pl-7 tabular-nums")}
      />
    </div>
  );
}

// Parse a user-typed money/number string to a number for the typed compute
// bodies. Blank → 0. Non-numeric → 0 (the backend re-validates regardless).
function num(v: string): number {
  const n = parseFloat(v.replace(/[,$\s]/g, ""));
  return Number.isFinite(n) ? n : 0;
}

// Same, but preserves "blank" as null for optional W-4P fields that the
// backend treats as nullable (estimated_annual_income, other_income_4a, …).
function numOrNull(v: string): number | null {
  if (v.trim() === "") return null;
  return num(v);
}

const CURRENT_YEAR = new Date().getFullYear();

export type PrepareClientProps = {
  workspaceId: string;
  entityId: string;
  kind: string;
};

export function PrepareClient({
  workspaceId,
  entityId,
  kind,
}: PrepareClientProps) {
  const [spec, setSpec] = useState<FormSpecDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setLoadError(null);
    forms
      .getFormSpec(kind)
      .then((s) => {
        if (alive) setSpec(s);
      })
      .catch((e) => {
        if (alive) setLoadError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [kind]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-default bg-surface px-5 py-8 text-sm text-ink-tertiary">
        <Loader2 size={15} className="animate-spin" />
        Loading the form…
      </div>
    );
  }

  if (loadError || !spec) {
    return (
      <ErrorState
        title="Couldn't load this form"
        description={
          loadError?.includes("404") || loadError?.toLowerCase().includes("not found")
            ? `There is no registered form with the kind "${kind}". Go back to the Forms hub and pick one.`
            : loadError ?? "The form specification could not be loaded."
        }
        recoveryAction={
          <Link
            href="/taxes/forms"
            className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs font-medium text-ink-secondary hover:text-ink-primary"
          >
            <ArrowLeft size={13} /> Back to forms
          </Link>
        }
      />
    );
  }

  return (
    <div className="space-y-5">
      {/* Header: form title + where it goes + back link */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <Link
            href="/taxes/forms"
            className="inline-flex items-center gap-1.5 text-xs text-ink-tertiary transition hover:text-ink-secondary"
          >
            <ArrowLeft size={13} /> All forms
          </Link>
          <h2 className="mt-2 flex items-center gap-2 text-lg font-semibold text-ink-primary">
            <FileText size={18} className="shrink-0 text-brand-400" />
            <span className="min-w-0">{spec.title}</span>
          </h2>
          <p className="mt-1 text-xs text-ink-tertiary">
            Goes to:{" "}
            <span className="font-medium text-ink-secondary">{spec.goes_to}</span>
          </p>
        </div>
      </div>

      {spec.mode === "computed" && spec.kind === "1040" ? (
        <Compute1040Form
          workspaceId={workspaceId}
          entityId={entityId}
          spec={spec}
        />
      ) : spec.mode === "computed" && spec.kind === "w-4p" ? (
        <ComputeW4pForm
          workspaceId={workspaceId}
          entityId={entityId}
          spec={spec}
        />
      ) : (
        <GenericFieldsForm
          workspaceId={workspaceId}
          entityId={entityId}
          spec={spec}
        />
      )}
    </div>
  );
}

// ===========================================================================
// Shared submit affordances.
// ===========================================================================

function SubmitBar({
  pending,
  label = "Prepare draft",
  error,
}: {
  pending: boolean;
  label?: string;
  error: string | null;
}) {
  return (
    <div className="space-y-3">
      {error ? (
        <div className="flex items-start gap-2 rounded-md border border-debit/40 bg-debit/[0.07] px-4 py-3">
          <AlertTriangle size={14} className="mt-0.5 shrink-0 text-debit" />
          <p className="text-[12px] leading-relaxed text-debit">{error}</p>
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={pending}
          className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-elevated disabled:text-ink-disabled"
        >
          {pending ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <Check size={14} />
          )}
          {pending ? "Preparing…" : label}
        </button>
        <Link
          href="/taxes/forms"
          className="rounded-md border border-subtle px-3 py-2 text-sm text-ink-secondary transition hover:bg-white/[0.04]"
        >
          Cancel
        </Link>
      </div>
    </div>
  );
}

/** A short success confirmation shown briefly before the redirect fires. */
function CreatedNotice({
  computed,
}: {
  computed?: Record<string, string>;
}) {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-credit/30 bg-credit/[0.06] px-4 py-3.5">
      <Check size={16} className="mt-0.5 shrink-0 text-credit" />
      <div className="min-w-0 text-xs leading-relaxed text-credit">
        <p className="font-semibold">Draft prepared — opening it now…</p>
        {computed && Object.keys(computed).length > 0 ? (
          <dl className="mt-2 grid grid-cols-1 gap-x-6 gap-y-1 sm:grid-cols-2">
            {Object.entries(computed).map(([k, v]) => (
              <div key={k} className="flex items-baseline justify-between gap-3">
                <dt className="text-credit/80">{humanizeKey(k)}</dt>
                <dd className="font-medium tabular-nums">{v}</dd>
              </div>
            ))}
          </dl>
        ) : null}
      </div>
    </div>
  );
}

function humanizeKey(k: string): string {
  return k
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .replace(/\bAgi\b/i, "AGI")
    .replace(/\bSe\b/i, "SE");
}

const FILING_STATUS_1040: { value: string; label: string }[] = [
  { value: "single", label: "Single" },
  { value: "mfj", label: "Married filing jointly" },
  { value: "mfs", label: "Married filing separately" },
  { value: "hoh", label: "Head of household" },
  { value: "qss", label: "Qualifying surviving spouse" },
];

const FILING_STATUS_W4P: { value: string; label: string }[] = [
  { value: "single_or_mfs", label: "Single or married filing separately" },
  { value: "mfj_or_qss", label: "Married filing jointly or qualifying surviving spouse" },
  { value: "head_of_household", label: "Head of household" },
];

// ===========================================================================
// Form 1040 — guided income form (computed by the deterministic engine).
// ===========================================================================

// Ordered list of the Compute1040Input currency levers + their labels.
const LEVERS_1040: { key: keyof Compute1040Input; label: string; line?: string }[] = [
  { key: "wages", label: "Wages (W-2)", line: "1a" },
  { key: "taxable_interest", label: "Taxable interest", line: "2b" },
  { key: "ordinary_dividends", label: "Ordinary dividends", line: "3b" },
  { key: "qualified_dividends", label: "Qualified dividends", line: "3a" },
  { key: "ira_taxable", label: "IRA distributions (taxable)", line: "4b" },
  { key: "pensions_taxable", label: "Pensions & annuities (taxable)", line: "5b" },
  { key: "social_security_taxable", label: "Social Security (taxable)", line: "6b" },
  { key: "capital_gain_loss", label: "Capital gain or (loss)", line: "7" },
  { key: "other_income", label: "Other income", line: "8" },
  { key: "adjustments", label: "Adjustments to income", line: "10" },
  { key: "itemized_deduction", label: "Itemized deduction" },
  { key: "qbi_deduction", label: "Qualified business income deduction", line: "13" },
  { key: "nonrefundable_credits", label: "Nonrefundable credits", line: "22" },
  { key: "withholding", label: "Federal income tax withheld", line: "25" },
  { key: "estimated_payments", label: "Estimated tax payments", line: "26" },
];

const LEVER_HELP: Partial<Record<keyof Compute1040Input, string>> = {
  itemized_deduction: "Leave blank to use the standard deduction (the engine picks the larger).",
};

function Compute1040Form({
  workspaceId,
  entityId,
  spec,
}: {
  workspaceId: string;
  entityId: string;
  spec: FormSpecDetail;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [computed, setComputed] = useState<Record<string, string> | null>(null);

  const [taxYear, setTaxYear] = useState<string>(String(CURRENT_YEAR));
  const [filingStatus, setFilingStatus] = useState<string>("single");
  const [incomeSource, setIncomeSource] = useState<string>("client documents");
  const [amounts, setAmounts] = useState<Record<string, string>>({});

  function setAmount(key: string, v: string) {
    setAmounts((prev) => ({ ...prev, [key]: v }));
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const compute_1040: Compute1040Input = {
      filing_status: filingStatus,
      income_source: incomeSource.trim() || "client documents",
    };
    for (const lever of LEVERS_1040) {
      compute_1040[lever.key] = num(amounts[lever.key] ?? "") as never;
    }
    startTransition(async () => {
      try {
        const res = await forms.createWorkProduct(workspaceId, entityId, {
          kind: "1040",
          tax_year: parseInt(taxYear, 10),
          compute_1040,
        });
        setComputed(res.computed ?? null);
        router.push(`/taxes/work-products/${res.id}`);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not prepare the 1040.");
      }
    });
  }

  return (
    <form onSubmit={onSubmit} className="space-y-5">
      <DeterministicNote />

      <Card>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <Field label="Tax year" htmlFor="tax_year">
            <input
              id="tax_year"
              type="number"
              min={2018}
              max={CURRENT_YEAR + 1}
              value={taxYear}
              onChange={(e) => setTaxYear(e.target.value)}
              className={INPUT_CLS}
            />
          </Field>
          <Field label="Filing status" htmlFor="filing_status">
            <select
              id="filing_status"
              value={filingStatus}
              onChange={(e) => setFilingStatus(e.target.value)}
              className={INPUT_CLS}
            >
              {FILING_STATUS_1040.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="Income source"
            htmlFor="income_source"
            hint="A short note recording where these figures came from."
          >
            <input
              id="income_source"
              type="text"
              value={incomeSource}
              onChange={(e) => setIncomeSource(e.target.value)}
              placeholder="client documents"
              className={INPUT_CLS}
            />
          </Field>
        </div>
      </Card>

      <Card>
        <h3 className="text-sm font-semibold text-ink-primary">Income & deductions</h3>
        <p className="mt-1 text-xs text-ink-tertiary">
          Enter the figures from the client&apos;s documents. The engine computes
          AGI, taxable income, and tax from these — leave anything that does not
          apply blank.
        </p>
        <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {LEVERS_1040.map((lever) => (
            <Field
              key={lever.key}
              label={lever.label}
              tag={lever.line ? `Line ${lever.line}` : undefined}
              hint={LEVER_HELP[lever.key]}
              htmlFor={`l_${lever.key}`}
            >
              <CurrencyInput
                id={`l_${lever.key}`}
                value={amounts[lever.key] ?? ""}
                onChange={(v) => setAmount(lever.key, v)}
              />
            </Field>
          ))}
        </div>
      </Card>

      {computed ? <CreatedNotice computed={computed} /> : null}
      <SubmitBar pending={pending} label="Prepare 1040 draft" error={error} />
    </form>
  );
}

function DeterministicNote() {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-brand-500/30 bg-brand-500/[0.06] px-4 py-3.5">
      <ShieldCheck size={16} className="mt-0.5 shrink-0 text-brand-400" />
      <div className="min-w-0 text-xs leading-relaxed text-ink-secondary">
        <p className="font-semibold text-ink-primary">
          Figures are computed by the deterministic engine — never typed or guessed.
        </p>
        <p className="mt-1">
          You enter the source amounts (wages, interest, deductions). The engine
          computes <strong>AGI, taxable income, and tax</strong>. The taxpayer&apos;s
          SSN and signature are left blank for them to complete.
        </p>
      </div>
    </div>
  );
}

// ===========================================================================
// Form W-4P — worksheet inputs (computed by the deterministic engine).
// ===========================================================================

const W4P_INT_FIELDS: {
  key: keyof ComputeW4pInput;
  label: string;
  nullable?: boolean;
  hint?: string;
}[] = [
  { key: "qualifying_children", label: "Qualifying children under 17" },
  { key: "other_dependents", label: "Other dependents" },
  { key: "other_credits_dollars", label: "Other tax credits ($)" },
  { key: "estimated_annual_income", label: "Estimated annual income", nullable: true },
  { key: "estimated_itemized_deductions", label: "Estimated itemized deductions" },
  { key: "estimated_adjustments", label: "Estimated adjustments to income" },
  { key: "other_income_4a", label: "Other income, not from jobs (4a)", nullable: true },
  {
    key: "extra_withholding_per_period",
    label: "Extra withholding per period (4c)",
    nullable: true,
  },
];

const W4P_TEXT_FIELDS: {
  key: keyof ComputeW4pInput;
  label: string;
  hint?: string;
}[] = [
  {
    key: "first_name_mi",
    label: "First name & middle initial",
    hint: "Leave name blank to auto-source it from the client's 1099-R.",
  },
  { key: "last_name", label: "Last name" },
  { key: "address", label: "Address" },
  { key: "city_state_zip", label: "City, state, ZIP" },
];

function ComputeW4pForm({
  workspaceId,
  entityId,
  spec,
}: {
  workspaceId: string;
  entityId: string;
  spec: FormSpecDetail;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const [taxYear, setTaxYear] = useState<string>(String(CURRENT_YEAR));
  const [filingStatus, setFilingStatus] = useState<string>("single_or_mfs");
  const [electNoWithholding, setElectNoWithholding] = useState(false);
  const [ints, setInts] = useState<Record<string, string>>({});
  const [texts, setTexts] = useState<Record<string, string>>({});

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const compute_w4p: ComputeW4pInput = {
      filing_status: filingStatus,
      elect_no_withholding: electNoWithholding,
    };
    for (const f of W4P_INT_FIELDS) {
      const raw = ints[f.key] ?? "";
      if (f.nullable) {
        compute_w4p[f.key] = numOrNull(raw) as never;
      } else {
        compute_w4p[f.key] = Math.round(num(raw)) as never;
      }
    }
    for (const f of W4P_TEXT_FIELDS) {
      const v = (texts[f.key] ?? "").trim();
      compute_w4p[f.key] = (v === "" ? null : v) as never;
    }
    startTransition(async () => {
      try {
        const res = await forms.createWorkProduct(workspaceId, entityId, {
          kind: "w-4p",
          tax_year: parseInt(taxYear, 10),
          compute_w4p,
        });
        setDone(true);
        router.push(`/taxes/work-products/${res.id}`);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not prepare the W-4P.");
      }
    });
  }

  return (
    <form onSubmit={onSubmit} className="space-y-5">
      <div className="flex items-start gap-3 rounded-lg border border-brand-500/30 bg-brand-500/[0.06] px-4 py-3.5">
        <ShieldCheck size={16} className="mt-0.5 shrink-0 text-brand-400" />
        <div className="min-w-0 text-xs leading-relaxed text-ink-secondary">
          <p className="font-semibold text-ink-primary">
            The withholding worksheet is computed for you.
          </p>
          <p className="mt-1">
            Enter the worksheet inputs below. The engine computes the W-4P
            withholding figures — the taxpayer signs Step 5 and gives page 1 to
            their pension payer (this form is <strong>never</strong> filed with the
            IRS).
          </p>
        </div>
      </div>

      <Card>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Tax year" htmlFor="w4p_tax_year">
            <input
              id="w4p_tax_year"
              type="number"
              min={2018}
              max={CURRENT_YEAR + 1}
              value={taxYear}
              onChange={(e) => setTaxYear(e.target.value)}
              className={INPUT_CLS}
            />
          </Field>
          <Field label="Filing status" htmlFor="w4p_filing_status">
            <select
              id="w4p_filing_status"
              value={filingStatus}
              onChange={(e) => setFilingStatus(e.target.value)}
              className={INPUT_CLS}
            >
              {FILING_STATUS_W4P.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </Card>

      <Card>
        <h3 className="text-sm font-semibold text-ink-primary">Worksheet figures</h3>
        <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {W4P_INT_FIELDS.map((f) => (
            <Field key={f.key} label={f.label} hint={f.hint} htmlFor={`w4p_${f.key}`}>
              <input
                id={`w4p_${f.key}`}
                type="number"
                inputMode="numeric"
                value={ints[f.key] ?? ""}
                onChange={(e) =>
                  setInts((prev) => ({ ...prev, [f.key]: e.target.value }))
                }
                placeholder="0"
                className={cn(INPUT_CLS, "tabular-nums")}
              />
            </Field>
          ))}
        </div>
      </Card>

      <Card>
        <h3 className="text-sm font-semibold text-ink-primary">Taxpayer details</h3>
        <p className="mt-1 text-xs text-ink-tertiary">
          Leave the name blank to auto-source it from the client&apos;s 1099-R.
        </p>
        <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
          {W4P_TEXT_FIELDS.map((f) => (
            <Field key={f.key} label={f.label} hint={f.hint} htmlFor={`w4p_${f.key}`}>
              <input
                id={`w4p_${f.key}`}
                type="text"
                value={texts[f.key] ?? ""}
                onChange={(e) =>
                  setTexts((prev) => ({ ...prev, [f.key]: e.target.value }))
                }
                className={INPUT_CLS}
              />
            </Field>
          ))}
        </div>

        <label className="mt-4 flex cursor-pointer items-start gap-2.5">
          <input
            type="checkbox"
            checked={electNoWithholding}
            onChange={(e) => setElectNoWithholding(e.target.checked)}
            className="mt-0.5 h-4 w-4 shrink-0 rounded border-default bg-elevated accent-brand-500"
          />
          <span className="text-xs leading-relaxed text-ink-secondary">
            Elect <strong>no withholding</strong> (the taxpayer wants $0 federal
            income tax withheld from these payments).
          </span>
        </label>
      </Card>

      {done ? <CreatedNotice /> : null}
      <SubmitBar pending={pending} label="Prepare W-4P draft" error={error} />
    </form>
  );
}

// ===========================================================================
// Generic fill / letter form — driven by spec.fields (FormFieldMeta[]).
// ===========================================================================

type ExtraRow = { id: number; key: string; value: string };

function GenericFieldsForm({
  workspaceId,
  entityId,
  spec,
}: {
  workspaceId: string;
  entityId: string;
  spec: FormSpecDetail;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const [taxYear, setTaxYear] = useState<string>(String(CURRENT_YEAR));
  const [title, setTitle] = useState<string>("");
  // Primary field values: text/currency/etc. stored as strings; checkboxes
  // stored as booleans.
  const [values, setValues] = useState<Record<string, string>>({});
  const [checks, setChecks] = useState<Record<string, boolean>>({});
  // Advanced repeater for fields not in the primary set.
  const [extras, setExtras] = useState<ExtraRow[]>([]);
  const [nextExtraId, setNextExtraId] = useState(1);

  function setValue(key: string, v: string) {
    setValues((prev) => ({ ...prev, [key]: v }));
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    const fields: Record<string, FieldInput> = {};

    // Primary fields by type.
    for (const f of spec.fields) {
      if (f.type === "checkbox") {
        if (checks[f.key]) {
          fields[f.key] = {
            value: "true",
            source_ref: "user_entered",
            origin: "user_entered",
          };
        }
        continue;
      }
      const raw = (values[f.key] ?? "").trim();
      if (raw === "") continue;
      fields[f.key] = {
        value: String(raw),
        source_ref: "user_entered",
        origin: "user_entered",
      };
    }

    // Advanced extra {key, value} rows. Last non-blank value for a key wins.
    for (const row of extras) {
      const key = row.key.trim();
      const value = row.value.trim();
      if (key === "" || value === "") continue;
      fields[key] = {
        value: String(value),
        source_ref: "user_entered",
        origin: "user_entered",
      };
    }

    const parsedYear = taxYear.trim() === "" ? null : parseInt(taxYear, 10);

    startTransition(async () => {
      try {
        const res = await forms.createWorkProduct(workspaceId, entityId, {
          kind: spec.kind,
          tax_year: parsedYear,
          title: title.trim() || null,
          fields,
        });
        setDone(true);
        router.push(`/taxes/work-products/${res.id}`);
      } catch (err) {
        setError(
          err instanceof Error ? err.message : `Could not prepare the ${spec.kind}.`,
        );
      }
    });
  }

  const hasPrimary = spec.fields.length > 0;

  return (
    <form onSubmit={onSubmit} className="space-y-5">
      <AntiFabricationNote />

      <Card>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field
            label="Tax year"
            htmlFor="gen_tax_year"
            hint="Optional — the period this form covers."
          >
            <input
              id="gen_tax_year"
              type="number"
              min={2000}
              max={CURRENT_YEAR + 1}
              value={taxYear}
              onChange={(e) => setTaxYear(e.target.value)}
              className={INPUT_CLS}
            />
          </Field>
          <Field
            label="Document title"
            htmlFor="gen_title"
            hint="Optional — defaults to the form name and tax year."
          >
            <input
              id="gen_title"
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder={`${spec.kind.toUpperCase()} ${taxYear}`.trim()}
              className={INPUT_CLS}
            />
          </Field>
        </div>
      </Card>

      {hasPrimary ? (
        <Card>
          <h3 className="text-sm font-semibold text-ink-primary">Form fields</h3>
          <p className="mt-1 text-xs text-ink-tertiary">
            Fill the figures you have. Blank fields stay blank on the form.
          </p>
          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
            {spec.fields.map((f) => (
              <GenericField
                key={f.key}
                meta={f}
                value={values[f.key] ?? ""}
                checked={checks[f.key] ?? false}
                onValue={(v) => setValue(f.key, v)}
                onCheck={(c) => setChecks((prev) => ({ ...prev, [f.key]: c }))}
              />
            ))}
          </div>
        </Card>
      ) : (
        <Card>
          <p className="text-sm text-ink-secondary">
            This form has no curated primary fields. Use{" "}
            <strong>Add another field</strong> below to enter values by their
            field key.
          </p>
        </Card>
      )}

      {/* Advanced repeater — arbitrary key/value pairs for the long tail. */}
      <Card>
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-ink-primary">
              Add another field{" "}
              <span className="font-normal text-ink-tertiary">(advanced)</span>
            </h3>
            <p className="mt-1 text-xs text-ink-tertiary">
              For fields not listed above. Use the exact renderer field key — an
              unknown key simply won&apos;t appear on the PDF.
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              setExtras((prev) => [
                ...prev,
                { id: nextExtraId, key: "", value: "" },
              ]);
              setNextExtraId((n) => n + 1);
            }}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs font-medium text-ink-secondary transition hover:text-ink-primary"
          >
            <Plus size={13} /> Add field
          </button>
        </div>

        {extras.length > 0 ? (
          <div className="mt-4 space-y-2">
            {extras.map((row) => (
              <div key={row.id} className="flex items-start gap-2">
                <input
                  type="text"
                  value={row.key}
                  onChange={(e) =>
                    setExtras((prev) =>
                      prev.map((r) =>
                        r.id === row.id ? { ...r, key: e.target.value } : r,
                      ),
                    )
                  }
                  placeholder="field_key"
                  aria-label="Field key"
                  className={cn(INPUT_CLS, "mt-0 font-mono")}
                />
                <input
                  type="text"
                  value={row.value}
                  onChange={(e) =>
                    setExtras((prev) =>
                      prev.map((r) =>
                        r.id === row.id ? { ...r, value: e.target.value } : r,
                      ),
                    )
                  }
                  placeholder="value"
                  aria-label="Field value"
                  className={cn(INPUT_CLS, "mt-0")}
                />
                <button
                  type="button"
                  onClick={() =>
                    setExtras((prev) => prev.filter((r) => r.id !== row.id))
                  }
                  aria-label="Remove field"
                  className="mt-1 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-subtle text-ink-tertiary transition hover:border-debit/40 hover:text-debit"
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
          </div>
        ) : null}
      </Card>

      {done ? <CreatedNotice /> : null}
      <SubmitBar pending={pending} label="Prepare draft" error={error} />
    </form>
  );
}

function GenericField({
  meta,
  value,
  checked,
  onValue,
  onCheck,
}: {
  meta: FormFieldMeta;
  value: string;
  checked: boolean;
  onValue: (v: string) => void;
  onCheck: (c: boolean) => void;
}) {
  const tag = meta.line ? `Line ${meta.line}` : undefined;
  const id = `f_${meta.key}`;

  if (meta.type === "checkbox") {
    return (
      <div className="sm:col-span-2">
        <label className="flex cursor-pointer items-start gap-2.5">
          <input
            id={id}
            type="checkbox"
            checked={checked}
            onChange={(e) => onCheck(e.target.checked)}
            className="mt-0.5 h-4 w-4 shrink-0 rounded border-default bg-elevated accent-brand-500"
          />
          <span className="min-w-0">
            <span className="flex items-center gap-2 text-xs font-medium text-ink-secondary">
              {meta.label}
              {tag ? (
                <span className="rounded-sm bg-white/[0.06] px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-ink-tertiary">
                  {tag}
                </span>
              ) : null}
            </span>
            {meta.help ? (
              <span className="mt-0.5 block text-[10px] leading-relaxed text-ink-tertiary">
                {meta.help}
              </span>
            ) : null}
          </span>
        </label>
      </div>
    );
  }

  if (meta.type === "currency") {
    return (
      <Field label={meta.label} tag={tag} hint={meta.help} htmlFor={id}>
        <CurrencyInput id={id} value={value} onChange={onValue} />
      </Field>
    );
  }

  // integer | percent → numeric; date → date; text | ssn | ein → text.
  const isNumeric = meta.type === "integer" || meta.type === "percent";
  const inputType =
    meta.type === "date" ? "date" : isNumeric ? "number" : "text";

  return (
    <Field label={meta.label} tag={tag} hint={meta.help} htmlFor={id}>
      <input
        id={id}
        type={inputType}
        inputMode={isNumeric ? "numeric" : undefined}
        value={value}
        onChange={(e) => onValue(e.target.value)}
        placeholder={
          meta.type === "ssn"
            ? "XXX-XX-XXXX"
            : meta.type === "ein"
              ? "XX-XXXXXXX"
              : meta.type === "percent"
                ? "%"
                : undefined
        }
        className={cn(INPUT_CLS, isNumeric && "tabular-nums")}
      />
    </Field>
  );
}

function AntiFabricationNote() {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-brand-500/30 bg-brand-500/[0.06] px-4 py-3.5">
      <Info size={16} className="mt-0.5 shrink-0 text-brand-400" />
      <div className="min-w-0 text-xs leading-relaxed text-ink-secondary">
        <p className="font-semibold text-ink-primary">
          Every figure you enter is recorded as &ldquo;entered by the preparer.&rdquo;
        </p>
        <p className="mt-1">
          We never invent numbers — <strong>blank fields stay blank</strong> on
          the form. Each value is stored with its provenance so the draft can be
          reviewed and signed with confidence.
        </p>
      </div>
    </div>
  );
}

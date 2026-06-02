// Server-side client for the AR / Invoicing endpoints.
//
// Reads go through the FastAPI app backend with the user's NextAuth bearer.
// Mutations from the browser hit the catch-all proxy at
// /api/workspaces/[...slug] (which attaches the bearer + streams the response,
// including the PDF content-disposition), so no per-route proxy is needed.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type InvoiceStatus =
  | "draft"
  | "sent"
  | "viewed"
  | "partial"
  | "paid"
  | "overdue"
  | "void";

export type InvoiceAging = "current" | "1-30" | "31-60" | "61-90" | "90+" | "paid";

export type InvoiceListRow = {
  id: string;
  entity_id: string;
  entity_display_name: string | null;
  counterparty_id: string;
  customer_name: string | null;
  invoice_number: string;
  status: InvoiceStatus;
  currency: string;
  issue_date: string;
  due_date: string;
  subtotal: string;
  tax_total: string;
  total: string;
  balance_due: string;
  po_number: string | null;
  external_source: string;
  external_ref: string | null;
  aging: InvoiceAging;
};

export type InvoiceLine = {
  id: string;
  line_number: number;
  description: string;
  quantity: string;
  unit_price: string;
  amount: string;
  tax_code: string | null;
  tax_amount: string;
  revenue_account_id: string | null;
  revenue_account_canonical: string | null;
  source_ref: string | null;
};

export type InvoicePayment = {
  id: string;
  amount: string;
  method: string;
  received_date: string;
  recon_link_id: string | null;
  journal_entry_id: string | null;
  external_ref: string | null;
  memo: string | null;
  created_at: string;
};

export type InvoiceDetail = {
  id: string;
  entity_id: string;
  entity_display_name: string | null;
  customer_name: string | null;
  invoice_number: string;
  status: InvoiceStatus;
  currency: string;
  issue_date: string;
  due_date: string;
  net_terms: number | null;
  subtotal: string;
  discount_total: string;
  tax_total: string;
  total: string;
  balance_due: string;
  notes: string | null;
  po_number: string | null;
  external_source: string;
  external_ref: string | null;
  journal_entry_id: string | null;
  void_reason: string | null;
  hosted_pay_url: string | null;
  pdf_url: string | null;
  lines: InvoiceLine[];
  payments: InvoicePayment[];
};

export type InvoiceListResponse = {
  invoices: InvoiceListRow[];
  total: number;
};

export async function getInvoices(
  workspaceId: string,
  filters: { entityId?: string; status?: string; limit?: number } = {}
): Promise<InvoiceListResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  const params = new URLSearchParams();
  if (filters.entityId) params.set("entity_id", filters.entityId);
  if (filters.status) params.set("status", filters.status);
  params.set("limit", String(filters.limit ?? 200));
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/invoices?${params}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as InvoiceListResponse;
  } catch {
    return null;
  }
}

export async function getInvoiceDetail(
  workspaceId: string,
  invoiceId: string
): Promise<InvoiceDetail | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/invoices/${invoiceId}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as InvoiceDetail;
  } catch {
    return null;
  }
}

export function formatMoney(value: string | number, currency = "USD"): string {
  const n = typeof value === "string" ? parseFloat(value) : value;
  if (Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  const sym = currency === "USD" ? "$" : `${currency} `;
  return `${sign}${sym}${abs.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })}`;
}

/** Map an invoice status to a StatusChip tone (see components/primitives). */
export function statusTone(
  status: InvoiceStatus
): "credit" | "debit" | "pending" | "ink" | "agent" | "blocked" {
  switch (status) {
    case "paid":
      return "credit";
    case "partial":
    case "sent":
    case "viewed":
      return "pending";
    case "overdue":
      return "debit";
    case "void":
      return "blocked";
    default:
      return "ink"; // draft
  }
}

/** Map an aging bucket to a tone for the aging pill. */
export function agingTone(
  aging: InvoiceAging
): "credit" | "debit" | "pending" | "ink" {
  switch (aging) {
    case "paid":
      return "credit";
    case "current":
      return "ink";
    case "1-30":
      return "pending";
    default:
      return "debit"; // 31-60, 61-90, 90+
  }
}

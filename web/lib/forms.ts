// Shared client-side API for the multi-form tax UI (Forms hub, prepare screens,
// work-products list/detail, e-sign, e-file, engagement policy).
//
// All JSON calls go through the auth-injecting proxy at /api/proxy/[...path]
// (which forwards everything under /api/* to the backend with the Bearer token).
// The ONLY exceptions are binary streams: the filled PDF and the e-file XML,
// which have dedicated Next.js routes that don't corrupt bytes.
//
// Usage (in a "use client" component):
//   import * as forms from "@/lib/forms";
//   const { items } = await forms.listForms();
//   const wp = await forms.createWorkProduct(wid, eid, { kind: "1040", tax_year: 2025, compute_1040: {...} });

// ---------------------------------------------------------------------------
// Types — mirror the backend contracts.
// ---------------------------------------------------------------------------

export type FormMode = "computed" | "fill" | "letter";
export type FormCategory =
  | "individual"
  | "business"
  | "payroll"
  | "state"
  | "notice";

export type FormSpec = {
  kind: string;
  title: string;
  jurisdiction: string; // "federal" | state code
  mode: FormMode;
  goes_to: string;
  category: FormCategory | string;
};

export type FormFieldType =
  | "currency"
  | "integer"
  | "text"
  | "checkbox"
  | "date"
  | "ssn"
  | "ein"
  | "percent";

export type FormFieldMeta = {
  key: string;
  label: string;
  type: FormFieldType;
  line?: string;
  help?: string;
};

export type FormSpecDetail = FormSpec & { fields: FormFieldMeta[] };

export type WorkProductStatus =
  | "draft"
  | "self_approved"
  | "pro_reviewed"
  | "finalized";

export type WorkProduct = {
  id: string;
  kind: string;
  tax_year: number | null;
  title: string | null;
  status: WorkProductStatus;
  attestation_kind: string | null;
  attested_by_uc_uid?: string | null;
  attested_at?: string | null;
  source_extraction_id?: string | null;
  created_at: string | null;
  updated_at?: string | null;
};

export type WorkProductDetail = WorkProduct & {
  entity_id?: string;
  inputs?: Record<string, { value: string; source_ref: string; origin?: string }>;
  provenance?: Record<string, unknown>;
};

export type ReviewMode = "pro_required" | "self_attest_allowed" | "autonomous_off";

export type SubmissionChannels = {
  esign?: boolean;
  efile_irs?: boolean;
  state_portal?: boolean;
  eftps?: boolean;
};

export type EngagementPolicy = {
  workspace_id?: string;
  review_mode: ReviewMode;
  has_licensed_pro: boolean;
  prefer_local_inference?: boolean;
  submission_channels: SubmissionChannels;
  rail_kill_switch: boolean;
  valid_review_modes?: ReviewMode[];
  rail_channels?: string[];
};

export type SignatureRequestStatus =
  | "created"
  | "sent"
  | "out_for_signature"
  | "signed"
  | "declined"
  | "canceled";

export type SignatureRequest = {
  id: string;
  work_product_id: string;
  provider: "docuseal" | "manual" | string;
  external_id: string | null;
  status: SignatureRequestStatus;
  signer_name: string | null;
  signer_email: string | null;
  signing_url: string | null;
  signed_garage_key?: string | null;
  signed_path?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type EfilePackageResult = {
  ok: boolean;
  work_product_id: string;
  kind: string;
  tax_year: number | null;
  status: string;
  manifest: Record<string, unknown>;
  warnings: string[];
  transmitted: boolean;
  xml_path: string;
  note: string;
};

// Create-work-product request bodies (mirror app/api/work_products.py).
export type Compute1040Input = {
  filing_status: string;
  wages?: number;
  taxable_interest?: number;
  ordinary_dividends?: number;
  qualified_dividends?: number;
  ira_taxable?: number;
  pensions_taxable?: number;
  social_security_taxable?: number;
  capital_gain_loss?: number;
  other_income?: number;
  adjustments?: number;
  itemized_deduction?: number;
  qbi_deduction?: number;
  nonrefundable_credits?: number;
  withholding?: number;
  estimated_payments?: number;
  income_source?: string;
};

export type ComputeW4pInput = {
  filing_status: string;
  qualifying_children?: number;
  other_dependents?: number;
  other_credits_dollars?: number;
  estimated_annual_income?: number | null;
  estimated_itemized_deductions?: number;
  estimated_adjustments?: number;
  other_income_4a?: number | null;
  extra_withholding_per_period?: number | null;
  first_name_mi?: string | null;
  last_name?: string | null;
  address?: string | null;
  city_state_zip?: string | null;
  elect_no_withholding?: boolean;
  source_extraction_id?: string | null;
};

// A fill/letter field value: the figure + where it came from (anti-fabrication).
export type FieldInput = { value: string; source_ref: string; origin?: string };

export type CreateWorkProductBody = {
  kind: string;
  tax_year?: number | null;
  title?: string | null;
  compute_1040?: Compute1040Input;
  compute_w4p?: ComputeW4pInput;
  fields?: Record<string, FieldInput>;
};

export type CreateWorkProductResult = {
  ok: boolean;
  work_product_id: string;
  id: string;
  kind: string;
  tax_year: number | null;
  status: WorkProductStatus;
  download_path: string;
  computed?: Record<string, string>;
  summary?: string;
  field_count?: number;
};

// ---------------------------------------------------------------------------
// URL helpers.
// ---------------------------------------------------------------------------

const enc = encodeURIComponent;

/** The auth-injecting proxy base for a path under the backend's /api/. */
export function proxy(path: string): string {
  return `/api/proxy/api${path.startsWith("/") ? path : "/" + path}`;
}

export function wpBase(workspaceId: string, entityId: string): string {
  return `/workspaces/${enc(workspaceId)}/entities/${enc(entityId)}/work-products`;
}

/** Binary PDF — dedicated Next route (NOT the proxy, which corrupts bytes). */
export function pdfUrl(
  workspaceId: string,
  entityId: string,
  wpId: string,
  disposition: "inline" | "attachment" = "inline",
): string {
  return `/api/workspaces/${enc(workspaceId)}/entities/${enc(entityId)}/work-products/${enc(
    wpId,
  )}/document.pdf?disposition=${disposition}`;
}

/** Multipart signed-PDF upload — dedicated Next route (binary-safe body). */
export function markSignedUploadUrl(
  workspaceId: string,
  entityId: string,
  wpId: string,
  srId: string,
): string {
  return `/api/esign-upload/${enc(workspaceId)}/${enc(entityId)}/${enc(wpId)}/${enc(srId)}`;
}

// ---------------------------------------------------------------------------
// Low-level JSON fetch through the proxy.
// ---------------------------------------------------------------------------

async function jget<T>(path: string): Promise<T> {
  const res = await fetch(proxy(path), { cache: "no-store" });
  if (!res.ok) throw await asError(res);
  return (await res.json()) as T;
}

async function jsend<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(proxy(path), {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) throw await asError(res);
  return (await res.json()) as T;
}

async function asError(res: Response): Promise<Error> {
  let detail = `HTTP ${res.status}`;
  try {
    const j = (await res.json()) as { detail?: string; error?: string };
    detail = j.detail ?? j.error ?? detail;
  } catch {
    /* not JSON */
  }
  return new Error(detail);
}

// ---------------------------------------------------------------------------
// Catalog.
// ---------------------------------------------------------------------------

export function listForms(): Promise<{ items: FormSpec[] }> {
  return jget("/forms");
}

export function getFormSpec(kind: string): Promise<FormSpecDetail> {
  return jget(`/forms/${enc(kind)}`);
}

// ---------------------------------------------------------------------------
// Work products.
// ---------------------------------------------------------------------------

export function listWorkProducts(
  workspaceId: string,
  entityId: string,
  opts: { kind?: string; tax_year?: number; limit?: number } = {},
): Promise<{ items: WorkProduct[] }> {
  const qs = new URLSearchParams();
  if (opts.kind) qs.set("kind", opts.kind);
  if (opts.tax_year) qs.set("tax_year", String(opts.tax_year));
  if (opts.limit) qs.set("limit", String(opts.limit));
  const q = qs.toString();
  return jget(`${wpBase(workspaceId, entityId)}${q ? "?" + q : ""}`);
}

export function getWorkProduct(
  workspaceId: string,
  entityId: string,
  wpId: string,
): Promise<WorkProductDetail> {
  return jget(`${wpBase(workspaceId, entityId)}/${enc(wpId)}`);
}

export function createWorkProduct(
  workspaceId: string,
  entityId: string,
  body: CreateWorkProductBody,
): Promise<CreateWorkProductResult> {
  return jsend("POST", wpBase(workspaceId, entityId), body);
}

export function attestWorkProduct(
  workspaceId: string,
  entityId: string,
  wpId: string,
  notes?: string,
): Promise<{ ok: boolean; status: WorkProductStatus; attestation_kind: string }> {
  return jsend("POST", `${wpBase(workspaceId, entityId)}/${enc(wpId)}/attest`, {
    confirm: true,
    notes,
  });
}

// ---------------------------------------------------------------------------
// Engagement policy.
// ---------------------------------------------------------------------------

export function getEngagementPolicy(workspaceId: string): Promise<EngagementPolicy> {
  return jget(`/workspaces/${enc(workspaceId)}/engagement/policy`);
}

export function updateEngagementPolicy(
  workspaceId: string,
  patch: Partial<{
    review_mode: ReviewMode;
    has_licensed_pro: boolean;
    prefer_local_inference: boolean;
    submission_channels: SubmissionChannels;
    rail_kill_switch: boolean;
  }>,
): Promise<EngagementPolicy> {
  return jsend("POST", `/workspaces/${enc(workspaceId)}/engagement/policy`, patch);
}

// ---------------------------------------------------------------------------
// E-signature.
// ---------------------------------------------------------------------------

export function listSignatureRequests(
  workspaceId: string,
  entityId: string,
  wpId: string,
): Promise<{ items: SignatureRequest[] }> {
  return jget(`${wpBase(workspaceId, entityId)}/${enc(wpId)}/signature-requests`);
}

export function createSignatureRequest(
  workspaceId: string,
  entityId: string,
  wpId: string,
  body: { signer_name: string; signer_email?: string | null; subject?: string | null },
): Promise<SignatureRequest> {
  return jsend(
    "POST",
    `${wpBase(workspaceId, entityId)}/${enc(wpId)}/signature-requests`,
    body,
  );
}

export function getSignatureRequest(
  workspaceId: string,
  entityId: string,
  wpId: string,
  srId: string,
  sync = false,
): Promise<SignatureRequest> {
  return jget(
    `${wpBase(workspaceId, entityId)}/${enc(wpId)}/signature-requests/${enc(srId)}${
      sync ? "?sync=true" : ""
    }`,
  );
}

export function cancelSignatureRequest(
  workspaceId: string,
  entityId: string,
  wpId: string,
  srId: string,
): Promise<SignatureRequest> {
  return jsend(
    "POST",
    `${wpBase(workspaceId, entityId)}/${enc(wpId)}/signature-requests/${enc(srId)}/cancel`,
  );
}

/** Mark signed WITHOUT an uploaded artifact (wet/out-of-band signature). For
 *  the upload path, POST multipart to markSignedUploadUrl(...). */
export function markSignedNoFile(
  workspaceId: string,
  entityId: string,
  wpId: string,
  srId: string,
): Promise<SignatureRequest> {
  return jsend(
    "POST",
    `${wpBase(workspaceId, entityId)}/${enc(wpId)}/signature-requests/${enc(srId)}/mark-signed`,
  );
}

// ---------------------------------------------------------------------------
// E-file (generate package; transmission is gated on EFIN and stays off).
// ---------------------------------------------------------------------------

export function generateEfilePackage(
  workspaceId: string,
  entityId: string,
  wpId: string,
): Promise<EfilePackageResult> {
  return jsend("POST", `${wpBase(workspaceId, entityId)}/${enc(wpId)}/efile-package`);
}

/** The MeF-style XML is text — fetch it through the proxy and blob-download. */
export function efileXmlProxyUrl(
  workspaceId: string,
  entityId: string,
  wpId: string,
  disposition: "inline" | "attachment" = "attachment",
): string {
  return proxy(
    `${wpBase(workspaceId, entityId)}/${enc(wpId)}/efile-package.xml?disposition=${disposition}`,
  );
}

// ---------------------------------------------------------------------------
// Small display helpers reused across screens.
// ---------------------------------------------------------------------------

export const STATUS_LABEL: Record<WorkProductStatus, string> = {
  draft: "Draft",
  self_approved: "Self-certified",
  pro_reviewed: "Pro reviewed",
  finalized: "Finalized",
};

export const STATUS_TONE: Record<WorkProductStatus, string> = {
  draft: "bg-pending/15 text-pending",
  self_approved: "bg-credit/15 text-credit",
  pro_reviewed: "bg-credit/15 text-credit",
  finalized: "bg-credit/15 text-credit",
};

export const CATEGORY_LABEL: Record<string, string> = {
  individual: "Individual",
  business: "Business",
  payroll: "Payroll",
  state: "State",
  notice: "IRS Notices",
};

export function fmtDate(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return s;
  }
}

/** Download a blob from a same-origin URL with a friendly filename. */
export async function downloadFrom(url: string, filename: string): Promise<void> {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw await asError(res);
  const cd = res.headers.get("content-disposition");
  let name = filename;
  if (cd) {
    const m = cd.match(/filename="?([^";\n]+)"?/i);
    if (m) name = m[1];
  }
  const blob = await res.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(href);
}

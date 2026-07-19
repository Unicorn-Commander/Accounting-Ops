// Server-side helpers for the /documents surface. Fetches the uploaded-document
// list (metadata only — never the bytes) for an entity. Mirrors the auth +
// best-effort-null pattern used by web/lib/api.ts.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type UploadedDocument = {
  id: string;
  content_hash: string;
  mime_type: string;
  amount: string | null;
  currency_code: string | null;
  extracted_at: string | null;
  created_at: string;
  object_key: string;
  review_status: string | null;
  // Tax-document OCR result (PDFs run through the local vision VLM after upload).
  // null until extraction completes; doc_type 'other' = not a recognized tax form.
  doc_type: string | null;
  tax_year: number | null;
  extraction_status: string | null; // 'extracted' | 'failed' | null (not yet run)
};

export type DocumentList = {
  documents: UploadedDocument[];
  total: number;
};

export async function getDocuments(
  workspaceId: string,
  entityId: string,
  opts: { limit?: number; offset?: number } = {}
): Promise<DocumentList | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  const qs = new URLSearchParams();
  if (opts.limit) qs.set("limit", String(opts.limit));
  if (opts.offset) qs.set("offset", String(opts.offset));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/entities/${entityId}/documents${suffix}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json",
        },
        cache: "no-store",
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as DocumentList;
  } catch {
    return null;
  }
}

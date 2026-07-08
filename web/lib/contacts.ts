// Server-side client for the Contacts (counterparty) endpoints.
//
// Reads go through the FastAPI backend with the user's NextAuth bearer.
// Browser mutations (parse-preview + import) hit the catch-all proxy at
// /api/workspaces/[...slug], which attaches the bearer — see the import modal.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type Contact = {
  id: string;
  display_name: string;
  kind: string;
  email: string | null;
  phone: string | null;
  address: string | null;
  company: string | null;
  contact_id: string | null;
  source: string | null;
  created_at: string;
};

export type ContactsResponse = {
  items: Contact[];
  total: number;
};

// A normalized preview row returned by the parse endpoint (and re-sent, with
// the user's edits, to the import endpoint).
export type ParsedContact = {
  display_name: string;
  person_name: string | null;
  company: string | null;
  email: string | null;
  phone: string | null;
  address: string | null;
  suggested_kind: string;
  source: string;
};

export type ContactSources = {
  file: boolean;
  contact_ops: boolean;
};

export async function getContactSources(
  workspaceId: string
): Promise<ContactSources> {
  const session = await auth();
  const fallback: ContactSources = { file: true, contact_ops: false };
  if (!session?.accessToken) return fallback;
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/counterparties/sources`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return fallback;
    return (await res.json()) as ContactSources;
  } catch {
    return fallback;
  }
}

export async function getContacts(
  workspaceId: string,
  filters: { q?: string; kind?: string; limit?: number } = {}
): Promise<ContactsResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.kind) params.set("kind", filters.kind);
  params.set("limit", String(filters.limit ?? 500));
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/counterparties?${params}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as ContactsResponse;
  } catch {
    return null;
  }
}

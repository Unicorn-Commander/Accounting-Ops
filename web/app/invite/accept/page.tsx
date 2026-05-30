import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

type AcceptResult =
  | {
      status: "accepted";
      workspace_id: string;
      workspace_name: string;
      workspace_slug: string;
      role: string;
    }
  | {
      status: "needs_auth";
      workspace_name: string;
      workspace_slug: string;
      role: string;
      signup_url: string;
      detail: string;
    }
  | { status: "error"; detail: string };

async function callAccept(token: string): Promise<AcceptResult> {
  const session = await auth();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (session?.accessToken) {
    headers["Authorization"] = `Bearer ${session.accessToken}`;
  }
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/invitations/accept?token=${encodeURIComponent(token)}`,
      { headers, cache: "no-store" }
    );
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      return {
        status: "error",
        detail:
          (data && (data as { detail?: string }).detail) ||
          `Accept failed (${res.status})`,
      };
    }
    return data as AcceptResult;
  } catch (err) {
    return {
      status: "error",
      detail: `Accept failed: ${(err as Error).message}`,
    };
  }
}

export default async function InviteAcceptPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const params = await searchParams;
  const token = params?.token || "";

  if (!token) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader title="Accept invitation" />
          <Card>
            <p className="text-sm text-debit">
              This page needs a valid invitation token in the URL.
            </p>
          </Card>
        </PageShell>
      </AppShell>
    );
  }

  const result = await callAccept(token);

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Accept invitation"
          subtitle="One-click access to the workspace you were invited to."
        />
        <Card>
          {result.status === "accepted" ? (
            <div className="flex flex-col gap-3">
              <p className="text-sm">
                You're now a <strong>{result.role}</strong> in{" "}
                <strong>{result.workspace_name}</strong>.
              </p>
              <Link
                href="/dashboard"
                className="self-start rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white shadow-brand-glow hover:bg-brand-600"
              >
                Open dashboard
              </Link>
            </div>
          ) : result.status === "needs_auth" ? (
            <div className="flex flex-col gap-3">
              <p className="text-sm">
                You're invited to <strong>{result.workspace_name}</strong> as a{" "}
                <strong>{result.role}</strong>.
              </p>
              <p className="text-sm text-ink-secondary">{result.detail}</p>
              <a
                href={result.signup_url}
                className="self-start rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white shadow-brand-glow hover:bg-brand-600"
              >
                Sign in / create account
              </a>
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              <p className="text-sm text-debit">{result.detail}</p>
              <Link
                href="/dashboard"
                className="self-start text-sm text-ink-secondary hover:text-ink-primary"
              >
                Go to dashboard
              </Link>
            </div>
          )}
        </Card>
      </PageShell>
    </AppShell>
  );
}

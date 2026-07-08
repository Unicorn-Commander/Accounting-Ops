import Link from "next/link";
import { ArrowLeft, Hash, Shield } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { Card, MaterialityBadge, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { auth } from "@/lib/auth";
import { getActiveContext } from "@/lib/active-context";
import { formatAmount, shortHash, type JournalEntry } from "@/lib/journal";
import { shortDate } from "@/lib/transactions";
import { cn } from "@/lib/cn";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

async function getEntry(workspaceId: string, entryId: string): Promise<JournalEntry | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/journal-entries/${entryId}`,
    {
      headers: { Authorization: `Bearer ${session.accessToken}` },
      cache: "no-store"
    }
  );
  if (!res.ok) return null;
  return (await res.json()) as JournalEntry;
}

export default async function JournalEntryPage(props: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await props.params;
  const ctx = await getActiveContext();
  if (!ctx.activeWorkspace) {
    return (
      <AppShell>
        <PageShell>
          <p className="text-sm text-ink-tertiary">Sign in to view entry.</p>
        </PageShell>
      </AppShell>
    );
  }
  const entry = await getEntry(ctx.activeWorkspace.id, id);
  if (!entry) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader title="Entry not found" subtitle={`No journal entry with id ${id}`} />
          <Link href="/journal" className="text-brand-400 hover:underline">
            ← Back to journal
          </Link>
        </PageShell>
      </AppShell>
    );
  }

  const amt = parseFloat(entry.amount);
  return (
    <AppShell>
      <PageShell>
        <Link
          href="/journal"
          className="mb-4 inline-flex items-center gap-1 text-xs text-ink-tertiary hover:text-ink-primary"
        >
          <ArrowLeft size={11} /> Back to journal
        </Link>
        <SectionHeader
          title={entry.payee || entry.description || "Journal entry"}
          subtitle={`${shortDate(entry.effective_date)} · ${entry.entity_display_name ?? "—"} · ${entry.source ?? "—"}`}
          action={
            <div className="flex items-center gap-2 text-xs">
              {entry.status === "posted" ? (
                <StatusChip tone="credit">Posted</StatusChip>
              ) : (
                <StatusChip tone="pending">Reversed</StatusChip>
              )}
              {entry.materiality_tier && <MaterialityBadge level={entry.materiality_tier} />}
            </div>
          }
        />

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Card>
            <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-ink-tertiary">
              Double-entry
            </h3>
            <dl className="space-y-2 text-sm">
              <div className="flex items-center justify-between">
                <span className="text-ink-tertiary">Debit</span>
                <span className="font-mono text-debit">{entry.debit_account}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-ink-tertiary">Credit</span>
                <span className="font-mono text-credit">{entry.credit_account}</span>
              </div>
              <div className="flex items-center justify-between border-t border-subtle pt-2">
                <span className="text-ink-tertiary">Amount</span>
                <span
                  className={cn(
                    "font-mono text-lg tabular-nums",
                    amt >= 0 ? "text-credit" : "text-debit"
                  )}
                >
                  {formatAmount(entry.amount)} {entry.currency_code}
                </span>
              </div>
            </dl>
          </Card>

          <Card>
            <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-ink-tertiary">
              Source + provenance
            </h3>
            <dl className="space-y-2 text-sm">
              <KV label="Source" value={entry.source ?? "—"} />
              <KV label="Source ID" value={entry.source_external_id ?? "—"} mono />
              <KV label="Category" value={entry.category ?? "—"} />
              <KV label="Posted by" value={entry.identity ?? "—"} />
              {entry.approving_human_uc_uid && (
                <KV label="Approved by" value={entry.approving_human_uc_uid} />
              )}
              <KV label="Posted at" value={new Date(entry.posted_at).toLocaleString()} />
            </dl>
          </Card>
        </div>

        <Card className="mt-4">
          <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-ink-tertiary">
            Description + memo
          </h3>
          <p className="text-sm">{entry.description ?? "(no description)"}</p>
          {entry.memo && (
            <p className="mt-2 text-xs text-ink-secondary whitespace-pre-line">{entry.memo}</p>
          )}
          {entry.reasoning_trace && (
            <details className="mt-3">
              <summary className="cursor-pointer text-xs font-medium text-ink-tertiary hover:text-ink-primary">
                Reasoning trace
              </summary>
              <pre className="mt-2 whitespace-pre-wrap rounded-md bg-canvas px-3 py-2 text-[11px] font-mono text-ink-secondary">
                {entry.reasoning_trace}
              </pre>
            </details>
          )}
        </Card>

        <Card className="mt-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-tertiary">
              <Shield size={11} className="mr-1 inline" />
              Hash chain
            </h3>
            <span className="text-[10px] text-ink-tertiary">
              Per-entity SHA-256, append-only
            </span>
          </div>
          <dl className="space-y-2 text-sm">
            <KV
              label="Previous hash"
              value={entry.prev_hash_hex}
              mono
              dim={entry.prev_hash_hex.startsWith("00000000")}
            />
            <KV label="This hash" value={entry.this_hash_hex} mono highlight />
            {entry.reverses_entry_id && (
              <KV
                label="Reverses entry"
                value={entry.reverses_entry_id}
                mono
                link={`/journal/${entry.reverses_entry_id}`}
              />
            )}
          </dl>
        </Card>
      </PageShell>
    </AppShell>
  );
}

function KV({
  label,
  value,
  mono,
  highlight,
  dim,
  link
}: {
  label: string;
  value: string;
  mono?: boolean;
  highlight?: boolean;
  dim?: boolean;
  link?: string;
}) {
  const valEl = link ? (
    <Link href={link} className="text-brand-400 hover:underline">
      {value}
    </Link>
  ) : (
    <span
      className={cn(
        mono && "font-mono text-xs break-all",
        highlight && "text-credit",
        dim && "text-ink-tertiary"
      )}
    >
      {value}
    </span>
  );
  return (
    <div className="flex items-start justify-between gap-2">
      <span className="text-ink-tertiary shrink-0">{label}</span>
      {valEl}
    </div>
  );
}

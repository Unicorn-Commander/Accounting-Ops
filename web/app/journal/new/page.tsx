import { getActiveContext } from "@/lib/active-context";
import { AppShell } from "@/components/app-shell";
import { PageShell, SectionHeader } from "@/components/primitives";
import { ManualEntryForm } from "./manual-entry-form";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

async function getCoaForEntity(workspaceId: string, entityId: string) {
  const session = await auth();
  if (!session?.accessToken) return [];
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/coa?entity_id=${entityId}`,
      {
        headers: { Authorization: `Bearer ${session.accessToken}` },
        cache: "no-store"
      }
    );
    if (!res.ok) return [];
    const body = await res.json();
    return body.accounts ?? [];
  } catch {
    return [];
  }
}

export default async function NewJournalEntryPage() {
  const ctx = await getActiveContext();
  if (!ctx.activeWorkspace) {
    return (
      <AppShell>
        <PageShell>
          <p className="text-sm text-ink-tertiary">Sign in to create entries.</p>
        </PageShell>
      </AppShell>
    );
  }

  // Pre-load COA for whichever entity is active (fallback: first entity)
  const initialEntity = ctx.activeEntity?.id ?? ctx.entities[0]?.id ?? "";
  const coa = initialEntity
    ? await getCoaForEntity(ctx.activeWorkspace.id, initialEntity)
    : [];

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="New journal entry"
          subtitle="Manual entries are append-only and join the same hash chain. Use for cash, owner contributions, reclasses, year-end adjustments."
        />
        <ManualEntryForm
          workspaceId={ctx.activeWorkspace.id}
          entities={ctx.entities.map((e) => ({ id: e.id, display_name: e.display_name }))}
          initialEntityId={initialEntity}
          initialCoa={coa}
        />
      </PageShell>
    </AppShell>
  );
}

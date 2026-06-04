// Cross-client CPA "Work Inbox" (v1).
//
// /clients shows cross-client COUNTS; this page shows the actual OPEN items —
// one merged, stalest-first list pulled from every workspace the caller can
// act on (GET /api/me/work-inbox). A CPA triages across all clients here and
// either acts inline (approvals) or deep-links into the client (tax reviews),
// instead of switching workspaces ~20 times.
//
// Server Component: fetch with the caller's token (web/lib/work-inbox.ts),
// then hand the payload to the client InboxView for the interactive list +
// inline-act. Degrades to a soft error state on a null fetch, like the
// dashboard does with the CFO brief.

import Link from "next/link";
import { ArrowLeft, Inbox } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EmptyState, PageShell, SectionHeader } from "@/components/primitives";
import { InboxView } from "@/app/clients/inbox/inbox-view";
import { fetchWorkInbox } from "@/lib/work-inbox";

export const dynamic = "force-dynamic";

export default async function WorkInboxPage() {
  const data = await fetchWorkInbox();

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Work inbox"
          subtitle="Every open item across all your clients — stalest first. Act inline or open the client."
          breadcrumb={
            <Link
              href="/clients"
              className="inline-flex items-center gap-1 text-xs text-ink-tertiary transition hover:text-brand-400"
            >
              <ArrowLeft size={12} /> Back to Clients
            </Link>
          }
        />

        {!data ? (
          // Null == fetch failed / not signed in. Permission-denied tone keeps
          // the recovery path honest (re-auth / ask for a CPA role), matching
          // the /clients empty state.
          <EmptyState
            variant="permission-denied"
            icon={<Inbox size={28} className="text-ink-disabled" aria-hidden="true" />}
            title="Couldn't load your work inbox."
            description="We couldn't reach the inbox service. Refresh the page; if it persists, confirm you still have an accountant or CPA role on at least one client."
          />
        ) : (
          <InboxView data={data} />
        )}
      </PageShell>
    </AppShell>
  );
}

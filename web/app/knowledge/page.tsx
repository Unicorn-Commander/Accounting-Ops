// /knowledge — Tax knowledge browser.
//
// Server component: loads facets + an initial document list from
// /api/tax-knowledge/{topics,documents}, hands them to KnowledgeBrowser
// which provides search-as-you-type + facet filters + document detail view.

import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { BookOpen } from "lucide-react";
import {
  fetchKnowledgeDocuments,
  fetchKnowledgeFacets,
} from "@/lib/tax-knowledge";
import { KnowledgeBrowser } from "./knowledge-browser";

export const dynamic = "force-dynamic";

type SearchParams = {
  q?: string;
  jurisdiction?: string;
  topic?: string;
  tax_year?: string;
};

export default async function KnowledgePage(props: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await props.searchParams;
  const taxYearNum = sp.tax_year ? parseInt(sp.tax_year, 10) : undefined;

  const [facets, initialList] = await Promise.all([
    fetchKnowledgeFacets(),
    fetchKnowledgeDocuments({
      query: sp.q,
      jurisdiction: sp.jurisdiction,
      topic: sp.topic,
      tax_year: taxYearNum,
      limit: 30,
    }),
  ]);

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Tax knowledge"
          subtitle={
            facets
              ? `${facets.total_documents.toLocaleString()} authoritative documents · ${facets.jurisdictions.length} jurisdictions · updated weekly by the Knowledge Curator`
              : "Authoritative tax-rule reference indexed for AI-specialist citations."
          }
        />

        {!facets || facets.error ? (
          <Card>
            <div className="flex items-start gap-3">
              <BookOpen size={20} className="mt-0.5 shrink-0 text-ink-tertiary" />
              <div>
                <p className="text-sm font-medium text-ink-secondary">
                  Knowledge index unavailable.
                </p>
                <p className="mt-1 max-w-2xl text-xs text-ink-tertiary">
                  The tax-knowledge RAG index hasn&apos;t finished its first
                  ingestion run, or the database role lacks read access. The
                  Tax Knowledge Curator agent populates this index weekly. See
                  the runbook for{" "}
                  <code className="rounded bg-elevated px-1 py-0.5 font-mono text-[11px]">
                    make knowledge-refresh
                  </code>
                  .
                </p>
              </div>
            </div>
          </Card>
        ) : (
          <KnowledgeBrowser
            facets={facets}
            initialList={initialList}
            initialQuery={sp.q ?? ""}
            initialJurisdiction={sp.jurisdiction}
            initialTopic={sp.topic}
            initialTaxYear={taxYearNum}
          />
        )}
      </PageShell>
    </AppShell>
  );
}

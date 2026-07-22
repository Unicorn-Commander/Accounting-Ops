import type { Metadata } from "next";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { LegalDraftBanner } from "@/components/legal-draft-banner";
import { LegalFooterLinks } from "@/components/legal-footer-links";
import { PROSE_CLASSES } from "@/components/legal-doc-page";
import { SiteHeader } from "@/components/site-header";
import { LEGAL_DOCS, loadLegalMarkdown } from "@/lib/legal-docs";

export const metadata: Metadata = {
  title: "Legal (Draft) · Accounting-Ops",
  description:
    "Draft legal documents for Accounting-Ops: Terms, Privacy, Scope of Service, and DPA. Not reviewed by counsel and not in force.",
  robots: { index: false, follow: false },
};

export default async function LegalHubPage() {
  // Hub shows the primary docs as cards; Scope + DPA are rendered inline
  // (they have no dedicated top-level route).
  const scopeMd = await loadLegalMarkdown("scope");
  const dpaMd = await loadLegalMarkdown("dpa");

  return (
    <div className="min-h-screen flex flex-col bg-canvas">
      <SiteHeader variant="marketing" />
      <LegalDraftBanner />

      <main className="flex-1 px-4 py-8 md:px-6 md:py-10">
        <div className="mx-auto w-full max-w-3xl">
          <header className="mb-8">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-pending">
              Draft · awaiting counsel
            </p>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight text-ink-primary">
              Legal
            </h1>
            <p className="mt-2 text-sm text-ink-secondary leading-relaxed max-w-2xl">
              These documents are engineer-authored drafting inputs for counsel.
              None of them is in force, and none creates a contract or privacy
              commitment until legal review is complete and final copy replaces
              the drafts.
            </p>
          </header>

          <section aria-label="Legal documents" className="grid gap-3 sm:grid-cols-2">
            {LEGAL_DOCS.map((doc) => {
              const isTopLevel = doc.slug === "terms" || doc.slug === "privacy";
              return (
                <Link
                  key={doc.slug}
                  href={doc.href}
                  className="group rounded-lg border border-default bg-surface px-4 py-3.5 shadow-card transition hover:border-brand-500/40 hover:bg-elevated"
                >
                  <div className="flex items-center gap-2">
                    <h2 className="text-sm font-semibold text-ink-primary group-hover:text-brand-300">
                      {doc.title}
                    </h2>
                    <span className="rounded-full bg-pending/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-pending">
                      Draft
                    </span>
                  </div>
                  <p className="mt-1.5 text-xs text-ink-tertiary leading-relaxed">
                    {doc.description}
                  </p>
                  {isTopLevel && (
                    <p className="mt-2 text-[11px] text-brand-400">
                      Open full page →
                    </p>
                  )}
                </Link>
              );
            })}
          </section>

          <section
            id="scope-of-service"
            aria-labelledby="scope-heading"
            className="mt-12 scroll-mt-24"
          >
            <h2
              id="scope-heading"
              className="text-sm font-semibold uppercase tracking-wider text-ink-tertiary mb-4"
            >
              Scope of Service
            </h2>
            {scopeMd === null ? (
              <MissingStub title="Scope of Service" />
            ) : (
              <div className={PROSE_CLASSES}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{scopeMd}</ReactMarkdown>
              </div>
            )}
          </section>

          <section
            id="data-processing-agreement"
            aria-labelledby="dpa-heading"
            className="mt-14 scroll-mt-24 border-t border-subtle pt-10"
          >
            <h2
              id="dpa-heading"
              className="text-sm font-semibold uppercase tracking-wider text-ink-tertiary mb-4"
            >
              Data Processing Agreement
            </h2>
            {dpaMd === null ? (
              <MissingStub title="Data Processing Agreement" />
            ) : (
              <div className={PROSE_CLASSES}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{dpaMd}</ReactMarkdown>
              </div>
            )}
          </section>
        </div>
      </main>

      <footer className="border-t border-subtle px-4 py-5 md:px-6">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <LegalFooterLinks />
          <p className="text-[10px] text-ink-disabled">
            Assistive software · not professional advice
          </p>
        </div>
      </footer>
    </div>
  );
}

function MissingStub({ title }: { title: string }) {
  return (
    <div className="rounded-lg border border-pending/40 bg-pending/[0.07] px-5 py-5">
      <p className="text-xs font-semibold uppercase tracking-wide text-pending">
        Draft missing
      </p>
      <p className="mt-2 text-sm text-ink-secondary">
        No draft found for {title}. Legal prose was not invented.
      </p>
    </div>
  );
}

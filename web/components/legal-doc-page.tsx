import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { LegalDraftBanner } from "@/components/legal-draft-banner";
import { LegalFooterLinks } from "@/components/legal-footer-links";
import { SiteHeader } from "@/components/site-header";
import { cn } from "@/lib/cn";
import type { LegalDocMeta } from "@/lib/legal-docs";
import { LEGAL_DOCS } from "@/lib/legal-docs";

/** Prose styles aligned with draft-markdown-viewer design tokens. */
const PROSE_CLASSES = cn(
  "text-sm text-ink-primary leading-relaxed",
  "[&_h1]:text-xl [&_h1]:font-bold [&_h1]:mt-0 [&_h1]:mb-4 [&_h1]:pb-2 [&_h1]:border-b [&_h1]:border-default",
  "[&_h2]:text-base [&_h2]:font-semibold [&_h2]:mt-8 [&_h2]:mb-2 [&_h2]:pb-1 [&_h2]:border-b [&_h2]:border-subtle",
  "[&_h3]:text-[13px] [&_h3]:font-semibold [&_h3]:mt-5 [&_h3]:mb-1.5",
  "[&_h4]:text-sm [&_h4]:font-medium [&_h4]:mt-3 [&_h4]:mb-1",
  "[&_p]:mb-3",
  "[&_ul]:mb-3 [&_ul]:pl-5 [&_ul]:list-disc",
  "[&_ol]:mb-3 [&_ol]:pl-5 [&_ol]:list-decimal",
  "[&_li]:mb-1",
  "[&_table]:w-full [&_table]:text-xs [&_table]:mb-4 [&_table]:border-collapse",
  "[&_th]:bg-elevated [&_th]:text-ink-secondary [&_th]:font-semibold [&_th]:px-2 [&_th]:py-1.5 [&_th]:text-left [&_th]:border [&_th]:border-subtle",
  "[&_td]:px-2 [&_td]:py-1 [&_td]:border [&_td]:border-subtle [&_td]:align-top",
  "[&_tr:nth-child(even)_td]:bg-canvas/50",
  "[&_code]:font-mono [&_code]:text-[11px] [&_code]:bg-elevated [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded",
  "[&_pre]:font-mono [&_pre]:text-[11px] [&_pre]:bg-elevated [&_pre]:p-3 [&_pre]:rounded-md [&_pre]:mb-3 [&_pre]:overflow-x-auto [&_pre]:border [&_pre]:border-subtle",
  "[&_pre_code]:bg-transparent [&_pre_code]:p-0",
  "[&_blockquote]:border-l-4 [&_blockquote]:border-pending [&_blockquote]:pl-3 [&_blockquote]:py-0.5 [&_blockquote]:bg-pending/[0.05] [&_blockquote]:text-ink-secondary [&_blockquote]:mb-3",
  "[&_strong]:font-semibold [&_em]:italic",
  "[&_hr]:border-t [&_hr]:border-default [&_hr]:my-6",
  "[&_a]:text-brand-400 [&_a]:underline [&_a]:underline-offset-2"
);

export function LegalDocPage({
  meta,
  markdown,
}: {
  meta: LegalDocMeta;
  /** null → missing draft stub */
  markdown: string | null;
}) {
  return (
    <div className="min-h-screen flex flex-col bg-canvas">
      <SiteHeader variant="marketing" />
      <LegalDraftBanner />

      <main className="flex-1 px-4 py-8 md:px-6 md:py-10">
        <article className="mx-auto w-full max-w-3xl">
          <nav
            aria-label="Legal documents"
            className="mb-6 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-tertiary"
          >
            {LEGAL_DOCS.filter((d) => d.href.startsWith("/") && !d.href.includes("#")).map(
              (d) => (
                <Link
                  key={d.slug}
                  href={d.href}
                  className={cn(
                    "hover:text-ink-secondary underline-offset-2 hover:underline",
                    d.slug === meta.slug && "text-ink-primary font-medium"
                  )}
                  aria-current={d.slug === meta.slug ? "page" : undefined}
                >
                  {d.title}
                </Link>
              )
            )}
            <Link
              href="/legal"
              className="hover:text-ink-secondary underline-offset-2 hover:underline"
            >
              All legal
            </Link>
          </nav>

          {markdown === null ? (
            <MissingDraftStub title={meta.title} />
          ) : (
            <div className={PROSE_CLASSES}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
            </div>
          )}
        </article>
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

function MissingDraftStub({ title }: { title: string }) {
  return (
    <div className="rounded-lg border border-pending/40 bg-pending/[0.07] px-5 py-6">
      <p className="text-xs font-semibold uppercase tracking-wide text-pending">
        Draft missing
      </p>
      <h1 className="mt-2 text-lg font-semibold text-ink-primary">{title}</h1>
      <p className="mt-2 text-sm text-ink-secondary leading-relaxed">
        No draft markdown was found for this page. Legal prose was not invented.
        Source drafts live in{" "}
        <code className="font-mono text-[12px] text-ink-primary">docs/legal/</code>{" "}
        and are mirrored under{" "}
        <code className="font-mono text-[12px] text-ink-primary">
          web/content/legal/
        </code>
        .
      </p>
    </div>
  );
}

export { PROSE_CLASSES };

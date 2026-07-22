import type { Metadata } from "next";
import { LegalDocPage } from "@/components/legal-doc-page";
import { getLegalDocMeta, loadLegalMarkdown } from "@/lib/legal-docs";

export const metadata: Metadata = {
  title: "Terms of Service (Draft) · Accounting-Ops",
  description:
    "Draft Terms of Service for the hosted Accounting-Ops service. Not reviewed by counsel and not in force.",
  robots: { index: false, follow: false },
};

export default async function TermsPage() {
  const meta = getLegalDocMeta("terms");
  const markdown = await loadLegalMarkdown("terms");
  return <LegalDocPage meta={meta} markdown={markdown} />;
}

/**
 * Legal draft documents served at /terms, /privacy, /legal.
 *
 * Content lives under web/content/legal/ (mirrored from docs/legal/*.draft.md
 * so the Docker/standalone image, which only copies web/, can render them).
 * All documents are unreviewed drafts — UI always shows a DRAFT banner.
 */

import { readFile } from "node:fs/promises";
import path from "node:path";

export type LegalDocSlug = "terms" | "privacy" | "scope" | "dpa";

export type LegalDocMeta = {
  slug: LegalDocSlug;
  /** Filename under content/legal/ */
  file: string;
  /** Page H1 / nav label */
  title: string;
  /** Short description for cards + metadata */
  description: string;
  /** Public route path */
  href: string;
};

/** Canonical catalog of legal draft documents. */
export const LEGAL_DOCS: readonly LegalDocMeta[] = [
  {
    slug: "terms",
    file: "terms.md",
    title: "Terms of Service",
    description:
      "Commercial terms for the hosted Accounting-Ops service (draft — not in force).",
    href: "/terms",
  },
  {
    slug: "privacy",
    file: "privacy.md",
    title: "Privacy Policy",
    description:
      "What we collect, who we send it to, and how long we keep it (draft — not in force).",
    href: "/privacy",
  },
  {
    slug: "scope",
    file: "scope.md",
    title: "Scope of Service",
    description:
      "Plain-English statement of what Accounting-Ops does and does not do.",
    href: "/legal#scope-of-service",
  },
  {
    slug: "dpa",
    file: "dpa.md",
    title: "Data Processing Agreement",
    description:
      "Controller/processor terms and subprocessor list (draft — not in force).",
    href: "/legal#data-processing-agreement",
  },
] as const;

const BY_SLUG: Record<LegalDocSlug, LegalDocMeta> = Object.fromEntries(
  LEGAL_DOCS.map((d) => [d.slug, d])
) as Record<LegalDocSlug, LegalDocMeta>;

export function getLegalDocMeta(slug: LegalDocSlug): LegalDocMeta {
  return BY_SLUG[slug];
}

/**
 * Load a legal draft markdown body from disk.
 * Returns null if the file is missing (caller should stub + report).
 */
export async function loadLegalMarkdown(
  slug: LegalDocSlug
): Promise<string | null> {
  const meta = BY_SLUG[slug];
  const filePath = path.join(process.cwd(), "content", "legal", meta.file);
  try {
    return await readFile(filePath, "utf8");
  } catch (err) {
    const code = (err as NodeJS.ErrnoException)?.code;
    if (code === "ENOENT") return null;
    throw err;
  }
}

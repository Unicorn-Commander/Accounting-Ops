// Hard-coded annotation store for per-entity-per-year CPA notes.
//
// Future: move this to a DB table (entity_tax_annotation or similar) so CPA
// notes can be edited in-app. For now a static map is sufficient because the
// known critical items are fully characterised.
//
// Key format: `${entitySlug}:${taxYear}` (lowercase slug, four-digit year).

export type TaxAnnotation = {
  /** Short headline shown in the overdue banner / card header. */
  headline: string;
  /** Full detail for the CPA notes section on the entity card. */
  detail: string;
  /** Optional tone for the card callout: 'critical' | 'warning' | 'info' */
  tone?: "critical" | "warning" | "info";
};

/** Annotation keyed by `${entitySlug}:${taxYear}`. */
const ANNOTATIONS: Record<string, TaxAnnotation> = {
  // MU Inc 2023 — SCDOR Failure-to-File (SC1120-CTD)
  "magic-unicorn-inc:2023": {
    headline: "SCDOR Failure-to-File notice (SC1120-CTD) — immediate action required",
    detail:
      "SCDOR issued a Failure-to-File notice for SC1120-CTD on 2025-06-16. The cure date has passed. " +
      "SC Voluntary Disclosure Agreement (VDA) MAY still be available if SCDOR has not made further contact — " +
      "escalate to CPA Mosin (NorthbridgeTax) immediately to assess VDA eligibility and stop further penalty accrual. " +
      "Federal 1120 also unfiled for 2023; both must be addressed in the same engagement.",
    tone: "critical",
  },

  // MU Inc 2022 — short year
  "magic-unicorn-inc:2022": {
    headline: "Short-year return — entity effective 2022-05-11",
    detail:
      "Magic Unicorn Inc was incorporated 2022-05-11. The 2022 SC1120 (and federal 1120) cover only the period " +
      "2022-05-11 through 2022-12-31. Original filing deadline was 2023-04-15. " +
      "Verify whether a short-year return was ever filed; if not, address alongside 2023 in the CPA engagement.",
    tone: "warning",
  },
};

/**
 * Return the annotation for a given entity slug + tax year, or null if none exists.
 * @param entitySlug  e.g. "magic-unicorn-inc"
 * @param taxYear     e.g. 2023
 */
export function getAnnotation(
  entitySlug: string,
  taxYear: number
): TaxAnnotation | null {
  const key = `${entitySlug.toLowerCase()}:${taxYear}`;
  return ANNOTATIONS[key] ?? null;
}

/**
 * Return all annotations that match a given entity slug (all years).
 */
export function getEntityAnnotations(
  entitySlug: string
): Array<TaxAnnotation & { tax_year: number }> {
  const slug = entitySlug.toLowerCase();
  const result: Array<TaxAnnotation & { tax_year: number }> = [];
  for (const [key, annotation] of Object.entries(ANNOTATIONS)) {
    const [kSlug, kYear] = key.split(":");
    if (kSlug === slug) {
      result.push({ ...annotation, tax_year: parseInt(kYear, 10) });
    }
  }
  return result.sort((a, b) => b.tax_year - a.tax_year);
}

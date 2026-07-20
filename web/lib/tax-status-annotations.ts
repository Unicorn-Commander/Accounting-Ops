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

/**
 * Annotation keyed by `${entitySlug}:${taxYear}`.
 *
 * COMMUNITY EDITION: this ships EMPTY so no operator's real per-entity tax
 * situation is baked into the source. Populate it (or, preferably, migrate to
 * the DB-backed store noted above) to surface CPA notes per entity/year.
 * Example shape:
 *   "acme-inc:2023": { headline: "…", detail: "…", tone: "warning" },
 */
const ANNOTATIONS: Record<string, TaxAnnotation> = {};

/**
 * Return the annotation for a given entity slug + tax year, or null if none exists.
 * @param entitySlug  e.g. "acme-inc"
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

// Pure banner state for the unmapped-accounts notice (AO111-R2-F5).
//
// GET /account-mappings needs accountant (AO-CLIENT-1). A 403 for a
// bookkeeper is NOT "0 unmapped accounts": the page says the mapping view is
// not available for the role instead of silently implying every account is
// mapped. Kept import-free so the web smoke tier can run it under tsx.

export type MappingsFetch =
  | { kind: "ok"; unmapped_count: number }
  | { kind: "forbidden" }
  | { kind: "unavailable" };

export type UnmappedBanner =
  | { kind: "unmapped"; count: number }
  | { kind: "not_for_role" }
  | { kind: "none" };

export function unmappedBanner(result: MappingsFetch | null): UnmappedBanner {
  if (result == null || result.kind === "unavailable") return { kind: "none" };
  if (result.kind === "forbidden") return { kind: "not_for_role" };
  return result.unmapped_count > 0
    ? { kind: "unmapped", count: result.unmapped_count }
    : { kind: "none" };
}

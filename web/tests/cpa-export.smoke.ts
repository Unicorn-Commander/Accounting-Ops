// Smoke test for the CPA Export feature.
//
// Run with `npx tsx tests/cpa-export.smoke.ts`.
//
// What it checks:
//   1. The lib helpers compute the expected outputs for sample inputs.
//   2. The page module, client module, and API route module all *import*
//      cleanly (no top-level throws).
//
// Why this matters: the page + route are server-only and pull in
// `next/headers` + `next-auth`. tsx executes module-level code, so this
// is a fast way to catch typos, missing exports, and bad import paths
// before `next build`.

async function main(): Promise<void> {
  // --- helpers ----------------------------------------------------------------
  const lib = await import("../lib/cpa-export");
  const { buildExportFilename, normalizePeriod, slugify, buildExportSearchParams, isValidPeriod } =
    lib;

  // slugify
  const slugCases: [string, string][] = [
    ["Magic Unicorn LLC!", "magic-unicorn-llc"],
    ["jane-smith", "jane-smith"],
    ["  hello   world  ", "hello-world"],
    ["Example Labs, Inc.", "example-labs-inc"]
  ];
  for (const [input, expected] of slugCases) {
    const got = slugify(input);
    if (got !== expected) {
      throw new Error(`slugify(${JSON.stringify(input)}) = ${JSON.stringify(got)}, expected ${JSON.stringify(expected)}`);
    }
  }

  // normalizePeriod
  const periodCases: [string, string][] = [
    ["ytd", "ytd"],
    ["YTD", "ytd"],
    ["mtd", "mtd"],
    ["qtd", "qtd"],
    ["2026", "2026"],
    ["2026-Q1", "2026-q1"],
    ["2026-q4", "2026-q4"],
    ["2026-05", "2026-05"],
    ["2026-01-01..2026-05-22", "2026-01-01-to-2026-05-22"],
    ["garbage", "ytd"],
    ["", "ytd"]
  ];
  for (const [input, expected] of periodCases) {
    const got = normalizePeriod(input);
    if (got !== expected) {
      throw new Error(`normalizePeriod(${JSON.stringify(input)}) = ${JSON.stringify(got)}, expected ${JSON.stringify(expected)}`);
    }
  }

  // isValidPeriod
  if (!isValidPeriod("2026-Q2")) throw new Error("isValidPeriod(2026-Q2) should be true");
  if (isValidPeriod("2026-Q5")) throw new Error("isValidPeriod(2026-Q5) should be false");
  if (isValidPeriod("nonsense")) throw new Error("isValidPeriod(nonsense) should be false");

  // buildExportFilename — covers all 6 backend formats.
  const ws = "jane-smith";
  const ent = "acme-inc";
  const filenameCases: [Parameters<typeof buildExportFilename>[0], string][] = [
    [
      { workspaceSlug: ws, entitySlug: ent, period: "2026-Q2", format: "csv" },
      "accounting-ops-jane-smith-acme-inc-2026-q2.csv"
    ],
    [
      { workspaceSlug: ws, entitySlug: ent, period: "ytd", format: "iif" },
      "accounting-ops-jane-smith-acme-inc-ytd.iif"
    ],
    [
      { workspaceSlug: ws, entitySlug: ent, period: "ytd", format: "qbo" },
      "accounting-ops-jane-smith-acme-inc-ytd.qbo"
    ],
    [
      { workspaceSlug: ws, entitySlug: ent, period: "ytd", format: "pl" },
      "accounting-ops-jane-smith-acme-inc-ytd.pl.pdf"
    ],
    [
      { workspaceSlug: ws, entitySlug: ent, period: "ytd", format: "balance-sheet" },
      "accounting-ops-jane-smith-acme-inc-ytd.balance-sheet.pdf"
    ],
    [
      { workspaceSlug: ws, entitySlug: ent, period: "ytd", format: "cash-flow" },
      "accounting-ops-jane-smith-acme-inc-ytd.cash-flow.pdf"
    ],
    [
      { workspaceSlug: ws, entitySlug: null, period: "ytd", format: "csv" },
      "accounting-ops-jane-smith-consolidated-ytd.csv"
    ],
    [
      { workspaceSlug: ws, entitySlug: ent, period: "2026-01-01..2026-05-22", format: "csv" },
      "accounting-ops-jane-smith-acme-inc-2026-01-01-to-2026-05-22.csv"
    ]
  ];
  for (const [args, expected] of filenameCases) {
    const got = buildExportFilename(args);
    if (got !== expected) {
      throw new Error(`buildExportFilename(${JSON.stringify(args)}) = ${JSON.stringify(got)}, expected ${JSON.stringify(expected)}`);
    }
  }

  // buildExportSearchParams
  const spA = buildExportSearchParams({
    workspaceId: "ws-uuid",
    period: "2026-Q2",
    entityId: "ent-uuid"
  });
  if (spA.get("workspace_id") !== "ws-uuid") throw new Error("workspace_id missing");
  if (spA.get("period") !== "2026-Q2") throw new Error("period missing");
  if (spA.get("entity_id") !== "ent-uuid") throw new Error("entity_id missing");
  const spB = buildExportSearchParams({
    workspaceId: "ws-uuid",
    period: "ytd",
    entityId: null
  });
  if (spB.get("entity_id") !== "all") throw new Error("null entity should map to 'all'");
  const spC = buildExportSearchParams({
    workspaceId: "ws-uuid",
    period: "ytd",
    entityId: "consolidated"
  });
  if (spC.get("entity_id") !== "all") throw new Error("'consolidated' entity should map to 'all'");

  // Form 1120 now crosses the app API so the Python assertion client binds
  // the canonical subject before calling the sidecar.
  const tax1120 = await import("../lib/taxes-1120");
  const entityUrl = tax1120.buildTax1120ApiUrl("ws-uuid", "entity-uuid", 2024);
  if (!entityUrl.endsWith("/api/workspaces/ws-uuid/entities/entity-uuid/tax/2024/form-1120")) {
    throw new Error(`unexpected entity Form 1120 URL: ${entityUrl}`);
  }
  const consolidatedUrl = tax1120.buildTax1120ApiUrl("ws-uuid", "all", 2024);
  if (!consolidatedUrl.endsWith("/api/workspaces/ws-uuid/consolidated/tax/2024/form-1120")) {
    throw new Error(`unexpected consolidated Form 1120 URL: ${consolidatedUrl}`);
  }
  const forbiddenHint = tax1120.taxPackageFailureHint(
    'cpa-export 403: {"detail":"No active membership for this workspace"}',
  );
  if (!forbiddenHint.includes("identity/membership mismatch between the app and cpa-export")) {
    throw new Error("403 Form 1120 hint should identify the app/sidecar identity mismatch");
  }

  const taxStatus = await import("../lib/tax-status");
  const statusPayload = {
    as_of: "2026-10-12",
    entities: [
      { entity_id: "entity-a", years: [] },
      { entity_id: "entity-b", years: [] }
    ]
  } as Parameters<typeof taxStatus.filterTaxStatusByEntity>[0];
  if (taxStatus.filterTaxStatusByEntity(statusPayload, "entity-b").entities.length !== 1) {
    throw new Error("entity tax-status filter should return one entity");
  }

  // --- module load ------------------------------------------------------------
  // The route module pulls in next-auth + NextResponse. Page + client pull in
  // React + Radix. We just need them to LOAD without throwing — that catches
  // typos, missing exports, and bad import paths cheaply.
  await import("../app/api/cpa-export/[...path]/route");
  await import("../app/cpa-export/cpa-export-client");
  await import("../app/cpa-export/page");

  console.log("cpa-export smoke OK");
}

main().catch((err) => {
  console.error("cpa-export smoke FAILED:", err);
  process.exit(1);
});

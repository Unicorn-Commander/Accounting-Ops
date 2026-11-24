// AO111-R2-F5: a 403 from GET /account-mappings is not "0 unmapped".
//
// Run with: `npx tsx tests/account-mappings-banner.smoke.ts` from web/.

export {};

async function main(): Promise<void> {
  const { unmappedBanner } = await import("../lib/account-mappings-banner");

  function expectKind(actual: { kind: string }, expected: string, label: string): void {
    if (actual.kind !== expected) {
      throw new Error(`${label}: expected ${expected}, got ${JSON.stringify(actual)}`);
    }
  }

  expectKind(unmappedBanner({ kind: "forbidden" }), "not_for_role", "403 for a bookkeeper");
  expectKind(unmappedBanner({ kind: "unavailable" }), "none", "fetch failed");
  expectKind(unmappedBanner(null), "none", "no workspace");
  expectKind(unmappedBanner({ kind: "ok", unmapped_count: 0 }), "none", "all mapped");
  const three = unmappedBanner({ kind: "ok", unmapped_count: 3 });
  expectKind(three, "unmapped", "three unmapped");
  if (three.kind === "unmapped" && three.count !== 3) {
    throw new Error(`three unmapped: count ${three.count}`);
  }
  console.log("account-mappings-banner smoke: ok");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

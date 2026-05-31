// Audit 2026-05-31 Bug 14 — integer-cents money parsing for the web UI.
//
// Run with: `npx tsx tests/money.smoke.ts` from the web/ directory.
//
// The old UI patterns (parseFloat + Math.round * 100) silently mis-round
// inputs like 1.005 (drops the trailing cent) and accumulate float drift
// when aggregating long transaction lists. The new lib/money.ts parses dollar
// strings into integer cents via string manipulation (never a float multiply)
// and rejects more than 2 decimal places with a clear MoneyParseError.

// Force this file to be treated as a module (not a global script) so the
// top-level `main()` doesn't collide with the identically-named function
// in cpa-export.smoke.ts under isolatedModules.
export {};

async function main(): Promise<void> {
  const { parseDollars, tryParseDollars, sumDollarsToCents, formatCents, MoneyParseError } =
    await import("../lib/money");

  function expectEqual<T>(actual: T, expected: T, label: string): void {
    if (actual !== expected) {
      throw new Error(
        `${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`
      );
    }
  }

  function expectThrows(fn: () => unknown, includes: string, label: string): void {
    let threw: Error | null = null;
    try {
      fn();
    } catch (e) {
      threw = e as Error;
    }
    if (!threw) throw new Error(`${label}: expected to throw`);
    if (!(threw instanceof MoneyParseError)) {
      throw new Error(
        `${label}: expected MoneyParseError, got ${threw.constructor.name}: ${threw.message}`
      );
    }
    if (!threw.message.includes(includes)) {
      throw new Error(
        `${label}: expected error message to contain ${JSON.stringify(includes)}, got ${JSON.stringify(threw.message)}`
      );
    }
  }

  // ---- happy path ---------------------------------------------------------
  expectEqual(parseDollars("0"), 0, "parseDollars('0')");
  expectEqual(parseDollars("1"), 100, "parseDollars('1')");
  expectEqual(parseDollars("1.5"), 150, "parseDollars('1.5')");
  expectEqual(parseDollars("1.50"), 150, "parseDollars('1.50')");
  expectEqual(parseDollars("0.01"), 1, "parseDollars('0.01')");
  expectEqual(parseDollars("0.99"), 99, "parseDollars('0.99')");
  expectEqual(parseDollars("12.34"), 1234, "parseDollars('12.34')");
  expectEqual(parseDollars("1234567.89"), 123456789, "parseDollars('1234567.89')");

  // ---- sign + symbols + commas + whitespace -------------------------------
  expectEqual(parseDollars("-1.50"), -150, "parseDollars('-1.50')");
  expectEqual(parseDollars("+1.50"), 150, "parseDollars('+1.50')");
  expectEqual(parseDollars("$1.50"), 150, "parseDollars('$1.50')");
  expectEqual(parseDollars("$-1.50"), -150, "parseDollars('$-1.50')");
  expectEqual(parseDollars("1,234.56"), 123456, "parseDollars('1,234.56')");
  expectEqual(parseDollars("  $1,234.56  "), 123456, "parseDollars(' $1,234.56 ')");
  expectEqual(parseDollars("1,000,000"), 100000000, "parseDollars('1,000,000')");

  // ---- the canary case: 1.005 must reject ---------------------------------
  expectThrows(() => parseDollars("1.005"), "more than 2 decimal places", "parseDollars('1.005')");
  expectThrows(
    () => parseDollars("0.001"),
    "more than 2 decimal places",
    "parseDollars('0.001')"
  );
  expectThrows(
    () => parseDollars("1.234"),
    "more than 2 decimal places",
    "parseDollars('1.234')"
  );

  // ---- other malformed inputs ---------------------------------------------
  expectThrows(() => parseDollars(""), "empty", "parseDollars('')");
  expectThrows(() => parseDollars("abc"), "not a valid", "parseDollars('abc')");
  // 1.2.3 has 3 chars after the FIRST dot so triggers the "too many decimals"
  // branch before the regex — both messages mean "invalid", just different specificity.
  expectThrows(() => parseDollars("1.2.3"), "decimal places", "parseDollars('1.2.3')");
  expectThrows(() => parseDollars("1e2"), "not a valid", "parseDollars('1e2')");
  expectThrows(() => parseDollars("."), "not a valid", "parseDollars('.')");
  expectThrows(() => parseDollars("1."), "not a valid", "parseDollars('1.')");
  expectThrows(() => parseDollars(".5"), "not a valid", "parseDollars('.5')");

  // ---- tryParseDollars never throws --------------------------------------
  expectEqual(tryParseDollars("1.50"), 150, "tryParseDollars('1.50')");
  expectEqual(tryParseDollars("garbage"), null, "tryParseDollars('garbage')");
  expectEqual(tryParseDollars("1.005"), null, "tryParseDollars('1.005')");

  // ---- aggregation precision (no float drift over long lists) ------------
  // Sum 100 entries of 0.10 each — must equal exactly 1000 cents (= $10.00).
  // The naive parseFloat path produces 9.999999999999998 here.
  const dimes = Array.from({ length: 100 }, () => "0.10");
  expectEqual(sumDollarsToCents(dimes), 1000, "sum of 100 × 0.10 cents");
  // 0.1 + 0.2 == 0.30 exactly (float would give 0.30000000000000004)
  expectEqual(sumDollarsToCents(["0.10", "0.20"]), 30, "sum of [0.10, 0.20]");

  // ---- formatCents round-trip --------------------------------------------
  expectEqual(formatCents(150), "1.50", "formatCents(150)");
  expectEqual(formatCents(0), "0.00", "formatCents(0)");
  expectEqual(formatCents(-150), "-1.50", "formatCents(-150)");
  expectEqual(formatCents(1), "0.01", "formatCents(1)");
  expectEqual(formatCents(99), "0.99", "formatCents(99)");
  expectEqual(formatCents(100), "1.00", "formatCents(100)");

  // parseDollars ∘ formatCents = identity (modulo sign canonicalization).
  for (const cents of [0, 1, 99, 100, 12345, -12345]) {
    const rt = parseDollars(formatCents(cents));
    if (rt !== cents) {
      throw new Error(
        `round-trip failed: cents=${cents} → format=${formatCents(cents)} → parse=${rt}`
      );
    }
  }

  console.log("money.smoke.ts: OK");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

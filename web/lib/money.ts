/**
 * Money parsing helpers for the web UI (audit 2026-05-31 Bug 14).
 *
 * The backend ledger is denominated in integer cents (or NUMERIC(20,4) for the
 * journal_entry.amount column). JavaScript `Number` is binary floating point and
 * cannot represent decimal cents exactly — `0.1 + 0.2 !== 0.3`. The old
 * UI patterns (`parseFloat(input)` + `Math.round(x * 100)`) round-trip OK for
 * most values but silently misrepresent values like `1.005` (rounds DOWN under
 * banker's rounding) and `0.1 + 0.2` (sums to `0.30000000000000004` cents).
 *
 * Approach: parse the input STRING with a regex that accepts at most 2 decimal
 * places, then build the integer cents by string manipulation — never via a
 * binary float multiply. Optional leading `$`, `+`, `-`, thousands separators,
 * and arbitrary whitespace are stripped before validation. Anything else is a
 * hard error so the caller surfaces a clean message to the user instead of
 * posting a wrong number.
 */

/** Thrown when a money string can't be parsed cleanly. */
export class MoneyParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MoneyParseError";
  }
}

/** Match an optional sign, an integer part (with optional commas), and at most
 * two decimal digits. The decimal part is OPTIONAL — '12' is 1200 cents.
 *
 * Examples that match:
 *   '1', '1.0', '1.00', '1.5', '0.01', '1,234.56', '-1.25', '+0.99'
 * Examples that do NOT match (rejected):
 *   '1.005', '1.2.3', 'abc', '1e2', '', '.', '1.', '0.001'
 */
const MONEY_REGEX = /^[+-]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?$/;

/**
 * Parse a dollar string into integer cents. Throws ``MoneyParseError`` on any
 * malformed input including:
 *   - empty string after trimming
 *   - more than 2 decimal places (rejects `1.005` → "max 2 decimal places")
 *   - non-numeric characters other than the allowed sign / `$` / `,`
 *
 * This NEVER calls parseFloat or Number() on the user-facing decimal portion;
 * all conversion is integer arithmetic to avoid binary-float drift.
 */
export function parseDollars(input: string): number {
  if (typeof input !== "string") {
    throw new MoneyParseError("Money input must be a string");
  }
  // Strip currency markers + thousands separators + whitespace.
  const cleaned = input.replace(/[\s$]/g, "");
  if (cleaned.length === 0) {
    throw new MoneyParseError("Money input is empty");
  }
  if (!MONEY_REGEX.test(cleaned)) {
    // Distinguish the common "too many decimals" case for a sharper message.
    const dotIdx = cleaned.indexOf(".");
    if (dotIdx >= 0 && cleaned.length - dotIdx - 1 > 2) {
      throw new MoneyParseError(
        "Money input has more than 2 decimal places (max 2)"
      );
    }
    throw new MoneyParseError(
      `Money input "${input}" is not a valid dollar amount`
    );
  }
  // Normalize: strip commas, leading +, and the leading - so we can parse parts.
  let body = cleaned.replace(/,/g, "");
  let sign = 1;
  if (body.startsWith("-")) {
    sign = -1;
    body = body.slice(1);
  } else if (body.startsWith("+")) {
    body = body.slice(1);
  }
  if (body.length === 0) {
    throw new MoneyParseError(`Money input "${input}" has no digits`);
  }
  const [whole, frac = ""] = body.split(".");
  if (whole.length === 0) {
    // The regex disallows leading dot, but be defensive.
    throw new MoneyParseError(
      `Money input "${input}" is missing the whole number part`
    );
  }
  const fracPadded = (frac + "00").slice(0, 2); // ensure exactly 2 digits
  // Safe integer arithmetic: whole * 100 + frac as parsed integers.
  const wholeCents = Number.parseInt(whole, 10) * 100;
  const fracCents = Number.parseInt(fracPadded, 10);
  if (!Number.isFinite(wholeCents) || !Number.isFinite(fracCents)) {
    throw new MoneyParseError(
      `Money input "${input}" overflowed safe integer range`
    );
  }
  const cents = sign * (wholeCents + fracCents);
  if (!Number.isSafeInteger(cents)) {
    throw new MoneyParseError(
      `Money input "${input}" exceeds JavaScript safe integer range`
    );
  }
  return cents;
}

/**
 * Parse but return null on any error (UI helper for inline validators that
 * just want a boolean ok/not-ok). Use ``parseDollars`` and catch
 * ``MoneyParseError`` when you need the specific failure reason.
 */
export function tryParseDollars(input: string): number | null {
  try {
    return parseDollars(input);
  } catch (_e) {
    return null;
  }
}

/** Sum a list of dollar amount STRINGS into integer cents, preserving exact
 * precision. Use this in `useMemo` instead of `parseFloat(...).reduce((s,x)=>s+x,0)`
 * which leaks float drift on long lists.
 */
export function sumDollarsToCents(values: readonly string[]): number {
  let total = 0;
  for (const v of values) {
    total += parseDollars(v);
  }
  return total;
}

/** Format integer cents back to a human-readable dollar string. The inverse of
 * parseDollars at the 2-decimal granularity. */
export function formatCents(
  cents: number,
  opts: { withSign?: boolean } = {}
): string {
  if (!Number.isFinite(cents)) return "0.00";
  const negative = cents < 0;
  const abs = Math.abs(cents);
  const whole = Math.trunc(abs / 100);
  const frac = abs % 100;
  const fracStr = frac < 10 ? `0${frac}` : `${frac}`;
  const sign = negative ? "-" : opts.withSign ? "+" : "";
  return `${sign}${whole}.${fracStr}`;
}

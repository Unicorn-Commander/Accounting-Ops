// Shared category-key list for UI dropdowns that let a human pick a
// Chart-of-Accounts category by its human-readable label.
//
// These are the keys of app.journal.posting.CATEGORY_TO_ACCOUNT — the only
// strings categorize_transaction / post_journal_entry will accept as
// `category`. They are NOT arbitrary COA account names or canonical_forms
// (those are "<number>·<name>", e.g. "6520·Meals" — a different string
// space). The backend also exposes GET /api/workspaces/{id}/approvals/categories
// (sorted CATEGORY_TO_ACCOUNT keys) but that route's read floor is
// `accountant`, one rung above the `bookkeeper` floor these categorize
// actions use — a bookkeeper would get a 403 fetching it. This constant
// mirrors that same key list so a bookkeeper-floor UI never depends on an
// accountant-floor read. Keep in sync with CATEGORY_TO_ACCOUNT
// (app/journal/posting.py) when categories are added there.
export const CATEGORY_OPTIONS = [
  "Revenue · Marketplace",
  "Revenue · Music",
  "Revenue · Services",
  "SaaS · AI",
  "SaaS · Email",
  "SaaS · Productivity",
  "Infrastructure · CDN",
  "Infrastructure · VPS",
  "Infrastructure · Network",
  "Banking · Fees",
  "Banking · Interest",
  "Inter-entity transfer",
  "Owner's Contribution",
  "Owner's Draw",
  "Travel · Lodging",
  "Travel · Transport",
  "Meals",
  "Office · Supplies",
  "Contractor · 1099-NEC",
  "Rent · Facilities",
  "Utilities",
  "Insurance",
  "Payroll · Wages",
  "Depreciation"
];

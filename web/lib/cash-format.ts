export type CashAccount = {
  id: string;
  name: string;
  org_name: string;
  currency: string;
  balance: string;
  available_balance: string | null;
  balance_as_of: number | null;
  last4: string | null;
  entity_id?: string | null;
  entity_display_name?: string | null;
  entity_slug?: string | null;
  account_type?: string | null;
  coa_canonical?: string | null;
};

export type CashPosition = {
  total_usd: string;
  currency: string;
  accounts: CashAccount[];
  configured: boolean;
  source?: string;
  message?: string;
  errors?: string[];
  assets_total?: string;
  liabilities_total?: string;
  net_total?: string;
  per_entity_totals?: Record<string, string>;
  per_entity_assets?: Record<string, string>;
  per_entity_liabilities?: Record<string, string>;
  unmapped_count?: number;
};

export function formatUSD(value: string | number): string {
  const n = typeof value === "string" ? parseFloat(value) : value;
  if (Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  return `${sign}$${abs.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })}`;
}

// Phase 1 connector registry. Each entry points at an in-stack MCP container
// reachable from the Next.js web container via the docker network.
//
// Health is fetched server-side; Phase 1.5 will add credential-capture UI
// (encrypted-at-rest in connector_state.credentials_secret_ref).

export type ConnectorStatus =
  | { state: "online"; configured: boolean; details: Record<string, unknown> }
  | { state: "offline"; message: string };

export type Connector = {
  id: string;
  name: string;
  category: "banking" | "marketplace" | "email" | "payroll" | "accounting";
  description: string;
  what_it_enables: string[];
  internal_url: string;
  monthly_cost: string;
  signup_url?: string;
  signup_label?: string;
  doc_url?: string;
  available: boolean; // true if the operator has built + deployed this MCP
};

export const CONNECTORS: Connector[] = [
  {
    id: "simplefin",
    name: "SimpleFIN Bridge",
    category: "banking",
    description:
      "Read-only banking aggregator. Covers Bank of America business, USAA personal, plus 1000+ other US institutions.",
    what_it_enables: [
      "Transaction history (last 90 days + ongoing)",
      "Real-time account balances",
      "Recurring-payment detection",
      "Transaction search by payee / memo"
    ],
    internal_url: "http://simplefin-mcp:8000",
    monthly_cost: "$1.50 per institution / month",
    signup_url: "https://beta-bridge.simplefin.org/",
    signup_label: "Get your Access URL at beta-bridge.simplefin.org",
    doc_url: "https://www.simplefin.org/protocol.html",
    available: true
  },
  {
    id: "ebay-finances",
    name: "eBay Finances",
    category: "marketplace",
    description:
      "Read-only access to the eBay Sell Finances API. Closes the BofA-deposit -> eBay-payout -> Listing-Ops-sale reconciliation loop.",
    what_it_enables: [
      "Payout history with status filtering",
      "Per-transaction detail (SALE/REFUND/DISPUTE/CREDIT/SHIPPING_LABEL)",
      "Seller funds summary (pending / on-hold / available)",
      "Reconcile single payout to underlying line items"
    ],
    internal_url: "http://ebay-finances-mcp:8000",
    monthly_cost: "Free (uses your eBay seller credentials)",
    signup_url: "https://developer.ebay.com/my/keys",
    signup_label: "Register your eBay app + get a refresh token",
    doc_url: "https://developer.ebay.com/api-docs/sell/finances/overview.html",
    available: true
  },
  {
    id: "plaid",
    name: "Plaid",
    category: "banking",
    description:
      "Polished banking aggregator. More features than SimpleFIN, more expensive, requires sales contact for production.",
    what_it_enables: [
      "Transactions + balances + identity + recurring + liabilities",
      "Per-bank OAuth flows",
      "Webhook-driven real-time updates"
    ],
    internal_url: "",
    monthly_cost: "$5-50+ / month + sales contact",
    available: false
  },
  {
    id: "stripe",
    name: "Stripe",
    category: "marketplace",
    description:
      "Read-only access to Stripe via a restricted API key. Pulls balance_transactions (charge/refund/fee/payout) and maps each to a double-entry journal entry. Closes the loop with SimpleFIN by matching each payout to its BoA deposit (±$1, ±2 days).",
    what_it_enables: [
      "Per-charge revenue with processor fee broken out (1210 → 4200; 5520 fee sidecar)",
      "Payouts auto-reconciled to BoA deposits via SimpleFIN",
      "Webhook-driven real-time event log (signature-verified)",
      "30-day backfill on first connect; first 30 rows queue for approval"
    ],
    internal_url: "",
    monthly_cost: "Free (uses your existing Stripe account)",
    signup_url: "https://dashboard.stripe.com/apikeys",
    signup_label: "Create a restricted API key (read-only)",
    doc_url: "https://stripe.com/docs/keys#limit-access-with-restricted-api-keys",
    available: true
  },
  {
    id: "quickbooks-online",
    name: "QuickBooks Online",
    category: "accounting",
    description:
      "Intuit-official MCP server. 144 tools across 29 entity types + 11 reports. Used for CPA-handoff export parity.",
    what_it_enables: [
      "Full Chart of Accounts CRUD",
      "Invoices, bills, payments, customers, vendors",
      "Standard reports (P&L, Balance Sheet, Cash Flow)"
    ],
    internal_url: "",
    monthly_cost: "Free (requires QBO subscription)",
    available: false
  },
  {
    id: "imap-receipts",
    name: "IMAP Receipts",
    category: "email",
    description:
      "Reads from a dedicated receipts@ mailbox on your own email server. Extracts attachments + parses receipt emails with confidence-scored heuristics.",
    what_it_enables: [
      "Folder + message listing with date / unseen / search filters",
      "Per-attachment SHA-256 + size cap + MIME-safe extraction",
      "extract_receipts: scored candidates (pdf, billing domain, subject, currency, localpart)",
      "Content-hash dedup keys for journal binding"
    ],
    internal_url: "http://imap-mcp:8000",
    monthly_cost: "Free (your own email server)",
    signup_label: "Create receipts@ mailbox on your existing mail server",
    doc_url: "https://datatracker.ietf.org/doc/html/rfc3501",
    available: true
  }
];

async function fetchHealth(url: string, timeoutMs = 3000): Promise<ConnectorStatus> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const res = await fetch(`${url}/healthz`, {
      cache: "no-store",
      signal: controller.signal
    });
    clearTimeout(timer);
    if (!res.ok) {
      return { state: "offline", message: `HTTP ${res.status}` };
    }
    const body = (await res.json()) as Record<string, unknown>;
    const configured = body.ok === true;
    return { state: "online", configured, details: body };
  } catch (err) {
    return {
      state: "offline",
      message: err instanceof Error ? err.message : "unknown error"
    };
  }
}

export async function getConnectorStatuses(): Promise<Record<string, ConnectorStatus>> {
  const out: Record<string, ConnectorStatus> = {};
  await Promise.all(
    CONNECTORS.filter((c) => c.available).map(async (c) => {
      out[c.id] = await fetchHealth(c.internal_url);
    })
  );
  return out;
}

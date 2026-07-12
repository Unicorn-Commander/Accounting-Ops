import {
  Activity,
  BarChart3,
  BookOpen,
  Cog,
  Contact,
  CreditCard,
  Database,
  FileSpreadsheet,
  FileText,
  HelpCircle,
  Home,
  Inbox,
  Key,
  ListChecks,
  type LucideIcon,
  FileCheck2,
  MessageSquare,
  Plug,
  Receipt,
  ShieldCheck,
  Target,
  TrendingUp,
  Upload,
  Users,
  Wallet,
} from "lucide-react";

export type NavItem = {
  label: string;
  href: string;
  icon: LucideIcon;
  /** HSL hue for the colored nav chip — e.g. "265 80% 65%" */
  hue: string;
  /** Hide from non-admin roles (enforced server-side too). */
  adminOnly?: boolean;
  /** Enterprise-edition only — hidden in Community Edition (route not mounted). */
  enterpriseOnly?: boolean;
};

export type NavSection = {
  label: string | null;
  items: NavItem[];
  /** Whole section is admin-only. */
  adminOnly?: boolean;
  /** Whole section is enterprise-edition only. */
  enterpriseOnly?: boolean;
  /** Collapsible default state. Secondary sections start collapsed. */
  defaultOpen?: boolean;
};

// Consolidated 7 → 5 sections. Admin + Admin-only folded into one Settings
// area (with per-item role gating); Help lives there too. Team + Settings
// start collapsed so the daily sections (Work, Books, Taxes) lead.
export const NAV_SECTIONS: NavSection[] = [
  {
    label: "Work",
    defaultOpen: true,
    items: [
      { label: "Dashboard", href: "/dashboard", icon: Home, hue: "265 80% 65%" },
      { label: "Open items", href: "/open-items", icon: Target, hue: "0 85% 68%" },
      { label: "Approvals", href: "/approvals", icon: Inbox, hue: "43 96% 56%" },
      { label: "Chat", href: "/chat", icon: MessageSquare, hue: "188 90% 56%" },
      { label: "Documents", href: "/documents", icon: Upload, hue: "200 90% 62%" },
      { label: "Activity log", href: "/agent-activity", icon: Activity, hue: "157 64% 52%" },
    ],
  },
  {
    label: "Books",
    defaultOpen: true,
    items: [
      { label: "Transactions", href: "/transactions", icon: Receipt, hue: "220 90% 68%" },
      { label: "Reconciliation", href: "/reconciliation-queue", icon: ListChecks, hue: "175 70% 50%" },
      { label: "Invoices", href: "/invoices", icon: FileCheck2, hue: "32 95% 60%" },
      { label: "Contacts", href: "/contacts", icon: Contact, hue: "330 80% 70%" },
      { label: "Journal", href: "/journal", icon: BookOpen, hue: "280 75% 70%" },
      { label: "Chart of Accounts", href: "/coa", icon: Database, hue: "280 65% 65%" },
      { label: "Reports", href: "/reports", icon: TrendingUp, hue: "150 65% 52%" },
      { label: "Trial Balance", href: "/reports/trial-balance", icon: BarChart3, hue: "150 60% 55%" },
    ],
  },
  {
    label: "Taxes",
    defaultOpen: true,
    items: [
      { label: "Taxes", href: "/taxes", icon: FileText, hue: "12 85% 63%" },
      { label: "Handoff", href: "/cpa-export", icon: FileSpreadsheet, hue: "32 80% 55%" },
    ],
  },
  {
    label: "Team",
    defaultOpen: false,
    items: [
      { label: "Members", href: "/settings/members", icon: Users, hue: "250 80% 72%" },
      // Multi-client CPA console — enterprise only (web/app/clients/** + the
      // clients/work_inbox routers are not mounted in Community Edition).
      { label: "Clients", href: "/clients", icon: Users, hue: "250 75% 65%", enterpriseOnly: true },
      { label: "Work inbox", href: "/clients/inbox", icon: Inbox, hue: "0 70% 60%", enterpriseOnly: true },
    ],
  },
  {
    label: "Settings",
    defaultOpen: false,
    items: [
      { label: "Connectors", href: "/connectors", icon: Plug, hue: "188 70% 50%" },
      { label: "Rules", href: "/specialty-rules", icon: Cog, hue: "210 60% 55%" },
      { label: "Sentinel", href: "/settings/sentinel", icon: ShieldCheck, hue: "157 55% 50%" },
      { label: "MCP Access Tokens", href: "/settings/pats", icon: Key, hue: "270 60% 60%" },
      { label: "Inference & billing", href: "/settings/inference", icon: CreditCard, hue: "157 60% 50%", adminOnly: true },
      { label: "Billing & Credits", href: "/settings/billing", icon: Wallet, hue: "43 96% 56%", adminOnly: true },
      { label: "Period Close", href: "/admin/period-close", icon: ShieldCheck, hue: "18 85% 55%", adminOnly: true },
      { label: "Help", href: "/help", icon: HelpCircle, hue: "240 20% 60%" },
    ],
  },
];

// The 4 highest-frequency destinations for the mobile bottom tab bar. The 5th
// slot ("More") opens the full grouped nav.
export const MOBILE_TABS: NavItem[] = [
  { label: "Home", href: "/dashboard", icon: Home, hue: "265 80% 65%" },
  { label: "Approvals", href: "/approvals", icon: Inbox, hue: "43 96% 56%" },
  { label: "Transactions", href: "/transactions", icon: Receipt, hue: "220 90% 68%" },
  { label: "Chat", href: "/chat", icon: MessageSquare, hue: "188 90% 56%" },
];

/**
 * Filter sections + items by role and edition; drop sections left empty.
 * In Community Edition (`isCommunity`), enterprise-only sections/items are
 * hidden — their routes are not mounted server-side either.
 */
export function visibleSections(
  canAdmin: boolean,
  isCommunity = false
): NavSection[] {
  return NAV_SECTIONS.filter((s) => !s.adminOnly || canAdmin)
    .filter((s) => !(isCommunity && s.enterpriseOnly))
    .map((s) => ({
      ...s,
      items: s.items.filter(
        (i) =>
          (!i.adminOnly || canAdmin) && !(isCommunity && i.enterpriseOnly)
      )
    }))
    .filter((s) => s.items.length > 0);
}

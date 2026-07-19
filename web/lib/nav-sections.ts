import {
  Activity,
  BarChart3,
  BookOpen,
  Cog,
  Contact,
  CreditCard,
  Database,
  FileSpreadsheet,
  FileStack,
  FileText,
  FilePen,
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
  Scale,
  ShieldCheck,
  Target,
  TrendingUp,
  Upload,
  Users,
} from "lucide-react";

export type NavItem = {
  label: string;
  href: string;
  icon: LucideIcon;
  /** HSL hue for the colored nav chip — e.g. "265 80% 65%" */
  hue: string;
  /** Hide from non-admin roles (enforced server-side too). */
  adminOnly?: boolean;
};

export type NavSection = {
  label: string | null;
  items: NavItem[];
  /** Whole section is admin-only. */
  adminOnly?: boolean;
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
      { label: "Forms", href: "/taxes/forms", icon: FileStack, hue: "16 84% 60%" },
      { label: "Work products", href: "/taxes/work-products", icon: FilePen, hue: "26 82% 58%" },
      { label: "W-4P", href: "/taxes/w-4p", icon: FileCheck2, hue: "22 82% 58%" },
      { label: "Handoff", href: "/cpa-export", icon: FileSpreadsheet, hue: "32 80% 55%" },
    ],
  },
  {
    label: "Team",
    defaultOpen: false,
    items: [
      { label: "Members", href: "/settings/members", icon: Users, hue: "250 80% 72%" },
      { label: "Clients", href: "/clients", icon: Users, hue: "250 75% 65%" },
      { label: "Work inbox", href: "/clients/inbox", icon: Inbox, hue: "0 70% 60%" },
    ],
  },
  {
    label: "Settings",
    defaultOpen: false,
    items: [
      { label: "Connectors", href: "/connectors", icon: Plug, hue: "188 70% 50%" },
      { label: "Rules", href: "/specialty-rules", icon: Cog, hue: "210 60% 55%" },
      { label: "Sentinel", href: "/settings/sentinel", icon: ShieldCheck, hue: "157 55% 50%" },
      { label: "Engagement & filing", href: "/settings/engagement", icon: Scale, hue: "262 70% 65%", adminOnly: true },
      { label: "MCP Access Tokens", href: "/settings/pats", icon: Key, hue: "270 60% 60%" },
      { label: "Inference & billing", href: "/settings/inference", icon: CreditCard, hue: "157 60% 50%", adminOnly: true },
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

/** Filter sections + items by role; drop sections left empty. */
export function visibleSections(canAdmin: boolean): NavSection[] {
  return NAV_SECTIONS.filter((s) => !s.adminOnly || canAdmin)
    .map((s) => ({ ...s, items: s.items.filter((i) => !i.adminOnly || canAdmin) }))
    .filter((s) => s.items.length > 0);
}

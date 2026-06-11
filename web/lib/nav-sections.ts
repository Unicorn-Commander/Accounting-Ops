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
} from "lucide-react";

export type NavItem = {
  label: string;
  href: string;
  icon: LucideIcon;
  /** HSL hue for the colored nav chip — e.g. "265 80% 65%" */
  hue: string;
};

export type NavSection = {
  label: string | null;
  items: NavItem[];
  adminOnly?: boolean;
};

export const NAV_SECTIONS: NavSection[] = [
  {
    label: "Work",
    items: [
      { label: "Dashboard", href: "/dashboard", icon: Home, hue: "265 80% 65%" },
      { label: "Documents", href: "/documents", icon: Upload, hue: "200 90% 62%" },
      { label: "Open items", href: "/open-items", icon: Target, hue: "0 85% 68%" },
      { label: "Chat", href: "/chat", icon: MessageSquare, hue: "188 90% 56%" },
      { label: "Approvals", href: "/approvals", icon: Inbox, hue: "43 96% 56%" },
      { label: "Activity log", href: "/agent-activity", icon: Activity, hue: "157 64% 52%" },
    ],
  },
  {
    label: "Books",
    items: [
      { label: "Transactions", href: "/transactions", icon: Receipt, hue: "220 90% 68%" },
      { label: "Contacts", href: "/contacts", icon: Contact, hue: "330 80% 70%" },
      { label: "Invoices", href: "/invoices", icon: FileCheck2, hue: "32 95% 60%" },
      { label: "Reconciliation", href: "/reconciliation-queue", icon: ListChecks, hue: "175 70% 50%" },
      { label: "Journal", href: "/journal", icon: BookOpen, hue: "280 75% 70%" },
      { label: "Chart of Accounts", href: "/coa", icon: Database, hue: "280 65% 65%" },
      { label: "Reports", href: "/reports", icon: TrendingUp, hue: "150 65% 52%" },
      { label: "Trial Balance", href: "/reports/trial-balance", icon: BarChart3, hue: "150 60% 55%" },
    ],
  },
  {
    label: "Taxes",
    items: [
      { label: "Taxes", href: "/taxes", icon: FileText, hue: "12 85% 63%" },
      { label: "Handoff", href: "/cpa-export", icon: FileSpreadsheet, hue: "32 80% 55%" },
    ],
  },
  {
    label: "Team",
    items: [
      { label: "Members", href: "/settings/members", icon: Users, hue: "250 80% 72%" },
      { label: "Clients", href: "/clients", icon: Users, hue: "250 75% 65%" },
      { label: "Work inbox", href: "/clients/inbox", icon: Inbox, hue: "0 70% 60%" },
    ],
  },
  {
    label: "Admin",
    items: [
      { label: "Rules", href: "/specialty-rules", icon: Cog, hue: "210 60% 55%" },
      { label: "Sentinel", href: "/settings/sentinel", icon: ShieldCheck, hue: "157 55% 50%" },
      { label: "MCP Access Tokens", href: "/settings/pats", icon: Key, hue: "270 60% 60%" },
      { label: "Connectors", href: "/connectors", icon: Plug, hue: "188 70% 50%" },
    ],
  },
  {
    label: "Admin only",
    adminOnly: true,
    items: [
      { label: "Inference & billing", href: "/settings/inference", icon: CreditCard, hue: "157 60% 50%" },
      { label: "Period Close", href: "/admin/period-close", icon: ShieldCheck, hue: "18 85% 55%" },
    ],
  },
  {
    label: null,
    items: [{ label: "Help", href: "/help", icon: HelpCircle, hue: "240 20% 60%" }],
  },
];

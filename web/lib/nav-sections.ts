// Single source of truth for app sidebar + mobile drawer navigation.
//
// Both `web/components/app-sidebar.tsx` (desktop) and
// `web/components/site-header-client.tsx` (mobile drawer) import from here.
// Adding/removing routes should happen in this file only.
//
// Each item carries a lucide icon (used by the desktop collapsed-rail) and a
// human-facing label. Sections labelled `Admin` are gated by `canAdmin`.

import {
  BarChart3,
  BookOpen,
  Cog,
  Database,
  FileSpreadsheet,
  FileText,
  HelpCircle,
  Home,
  Inbox,
  Key,
  ListChecks,
  type LucideIcon,
  MessageSquare,
  Plug,
  Receipt,
  ShieldCheck,
  Sparkles,
  Target,
  TrendingUp,
  Users,
} from "lucide-react";

export type NavItem = {
  label: string;
  href: string;
  icon: LucideIcon;
};

export type NavSection = {
  label: string | null;
  items: NavItem[];
  adminOnly?: boolean;
};

// Ordered top-to-bottom in the rendered sidebar. The first (unlabeled) section
// is the "work surface": daily action loop. Subsequent labeled sections group
// reporting, admin, and settings.
export const NAV_SECTIONS: NavSection[] = [
  {
    label: null,
    items: [
      { label: "Dashboard", href: "/dashboard", icon: Home },
      { label: "Open items", href: "/open-items", icon: Target },
      { label: "Chat", href: "/chat", icon: MessageSquare },
      { label: "Transactions", href: "/transactions", icon: Receipt },
      { label: "Reconciliation", href: "/reconciliation-queue", icon: ListChecks },
      { label: "Approvals", href: "/approvals", icon: Inbox },
      { label: "Activity log", href: "/agent-activity", icon: Sparkles },
      { label: "Journal", href: "/journal", icon: BookOpen },
      { label: "Chart of Accounts", href: "/coa", icon: Database },
    ],
  },
  {
    label: "Reports",
    items: [
      { label: "Reports", href: "/reports", icon: TrendingUp },
      { label: "Trial Balance", href: "/reports/trial-balance", icon: BarChart3 },
      { label: "Taxes", href: "/taxes", icon: FileText },
      { label: "CPA Export", href: "/cpa-export", icon: FileSpreadsheet },
    ],
  },
  {
    label: "Admin",
    adminOnly: true,
    items: [
      { label: "Period Close", href: "/admin/period-close", icon: ShieldCheck },
    ],
  },
  {
    label: "Settings",
    items: [
      { label: "Members", href: "/settings/members", icon: Users },
      { label: "Sentinel", href: "/settings/sentinel", icon: ShieldCheck },
      { label: "Access Tokens", href: "/settings/pats", icon: Key },
      { label: "Specialty Rules", href: "/specialty-rules", icon: Cog },
    ],
  },
  {
    label: null,
    items: [
      { label: "Connectors", href: "/connectors", icon: Plug },
      { label: "Help", href: "/help", icon: HelpCircle },
    ],
  },
];

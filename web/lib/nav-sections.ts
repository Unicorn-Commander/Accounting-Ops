// Single source of truth for app sidebar + mobile drawer navigation.
//
// Both `web/components/app-sidebar.tsx` (desktop) and
// `web/components/site-header-client.tsx` (mobile drawer) import from here.
// Adding/removing routes should happen in this file only.
//
// Each item carries a lucide icon (used by the desktop collapsed-rail) and a
// human-facing label. Sections labelled `Admin` are gated by `canAdmin`.
//
// I15 (Tier-2): groups reorganised from arbitrary buckets to job-based
// sections: Work, Books, Taxes, Team, Admin. Settings/admin pages are
// consolidated under one Admin area instead of being scattered across
// Settings/Reports/etc. Renamed labels:
//   Agent activity        -> Activity log    (already in Tier-1)
//   Specialty Rules       -> Rules
//   CPA Export            -> Handoff
//   Personal Access Tokens-> MCP Access Tokens

import {
  Activity,
  BarChart3,
  BookOpen,
  Cog,
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
};

export type NavSection = {
  label: string | null;
  items: NavItem[];
  adminOnly?: boolean;
};

// Ordered top-to-bottom in the rendered sidebar. Section groups follow the
// daily user mental model: Work (daily action loop) → Books (the ledger) →
// Taxes (CPA-facing) → Team (people + permissions) → Admin (system).
export const NAV_SECTIONS: NavSection[] = [
  {
    // Daily action loop — Dashboard + Open items + Chat + the approvals queue.
    label: "Work",
    items: [
      { label: "Dashboard", href: "/dashboard", icon: Home },
      { label: "Documents", href: "/documents", icon: Upload },
      { label: "Open items", href: "/open-items", icon: Target },
      { label: "Chat", href: "/chat", icon: MessageSquare },
      { label: "Approvals", href: "/approvals", icon: Inbox },
      { label: "Activity log", href: "/agent-activity", icon: Activity },
    ],
  },
  {
    // The bookkeeping surfaces — what you do TO the ledger.
    label: "Books",
    items: [
      { label: "Transactions", href: "/transactions", icon: Receipt },
      { label: "Invoices", href: "/invoices", icon: FileCheck2 },
      { label: "Reconciliation", href: "/reconciliation-queue", icon: ListChecks },
      { label: "Journal", href: "/journal", icon: BookOpen },
      { label: "Chart of Accounts", href: "/coa", icon: Database },
      { label: "Reports", href: "/reports", icon: TrendingUp },
      { label: "Trial Balance", href: "/reports/trial-balance", icon: BarChart3 },
    ],
  },
  {
    // Tax surfaces — CPA-facing.
    label: "Taxes",
    items: [
      { label: "Taxes", href: "/taxes", icon: FileText },
      // I15: "CPA Export" -> "Handoff" (the route stays /cpa-export so links
      // keep working).
      { label: "Handoff", href: "/cpa-export", icon: FileSpreadsheet },
    ],
  },
  {
    // People + permissions — managing who's in the workspace.
    label: "Team",
    items: [
      { label: "Members", href: "/settings/members", icon: Users },
      { label: "Clients", href: "/clients", icon: Users },
      // Cross-client CPA work inbox — the actionable companion to /clients.
      { label: "Work inbox", href: "/clients/inbox", icon: Inbox },
    ],
  },
  {
    // I15: One consolidated Admin area for all system/owner settings.
    // Sentinel + Period Close (admin-only) sit alongside Rules + MCP Tokens
    // so admins find configuration in one place.
    label: "Admin",
    items: [
      // I15: "Specialty Rules" -> "Rules"
      { label: "Rules", href: "/specialty-rules", icon: Cog },
      { label: "Sentinel", href: "/settings/sentinel", icon: ShieldCheck },
      // I15: "Personal Access Tokens" -> "MCP Access Tokens"
      { label: "MCP Access Tokens", href: "/settings/pats", icon: Key },
      { label: "Connectors", href: "/connectors", icon: Plug },
    ],
  },
  {
    // adminOnly section — gated by canAdmin (owner/client_owner/admin/
    // platform_admin) in the rendering components. The BYOK inference key +
    // (coming soon) credit/Claude-subscription billing live here so lower
    // roles never see the link; the page itself also re-checks the role.
    label: "Admin only",
    adminOnly: true,
    items: [
      { label: "Inference & billing", href: "/settings/inference", icon: CreditCard },
      { label: "Period Close", href: "/admin/period-close", icon: ShieldCheck },
    ],
  },
  {
    label: null,
    items: [{ label: "Help", href: "/help", icon: HelpCircle }],
  },
];

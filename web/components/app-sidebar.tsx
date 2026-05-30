"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import {
  BarChart3,
  BookOpen,
  ChevronsLeft,
  ChevronsRight,
  Cog,
  Database,
  FileSpreadsheet,
  FileText,
  HelpCircle,
  Home,
  Inbox,
  ListChecks,
  type LucideIcon,
  MessageSquare,
  Plug,
  Receipt,
  ShieldCheck,
  Sparkles,
  TrendingUp,
} from "lucide-react";
import { cn } from "@/lib/cn";

type NavItem = {
  label: string;
  href: string;
  icon: LucideIcon;
};

type NavSection = {
  label: string | null;
  items: NavItem[];
  adminOnly?: boolean;
};

// Order + grouping matches site-header NAV_SECTIONS for parity, but each item
// now has a dedicated lucide icon for the collapsed sidebar state.
const NAV_SECTIONS: NavSection[] = [
  {
    label: null,
    items: [
      { label: "Dashboard", href: "/dashboard", icon: Home },
      { label: "Chat", href: "/chat", icon: MessageSquare },
      { label: "Transactions", href: "/transactions", icon: Receipt },
      { label: "Reconciliation", href: "/reconciliation-queue", icon: ListChecks },
      { label: "Approvals", href: "/approvals", icon: Inbox },
      { label: "Agent activity", href: "/agent-activity", icon: Sparkles },
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
    items: [{ label: "Period Close", href: "/admin/period-close", icon: ShieldCheck }],
  },
  {
    label: null,
    items: [
      { label: "Connectors", href: "/connectors", icon: Plug },
      { label: "Specialty Rules", href: "/specialty-rules", icon: Cog },
      { label: "Help", href: "/help", icon: HelpCircle },
    ],
  },
];

const COOKIE = "aops_sidebar_collapsed";

function readCollapsedCookie(): boolean {
  if (typeof document === "undefined") return false;
  return document.cookie
    .split("; ")
    .some((c) => c.startsWith(`${COOKIE}=1`));
}

function writeCollapsedCookie(collapsed: boolean) {
  document.cookie = `${COOKIE}=${collapsed ? "1" : "0"}; Path=/; SameSite=Lax; Max-Age=31536000`;
}

export function AppSidebar({
  canAdmin = false,
  initialCollapsed = false,
}: {
  canAdmin?: boolean;
  initialCollapsed?: boolean;
}) {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(initialCollapsed);

  // Hydrate from cookie on the client in case SSR didn't see it.
  useEffect(() => {
    setCollapsed(readCollapsedCookie());
  }, []);

  const sections = NAV_SECTIONS.filter((s) => !s.adminOnly || canAdmin);

  function toggle() {
    const next = !collapsed;
    setCollapsed(next);
    writeCollapsedCookie(next);
  }

  return (
    <aside
      className={cn(
        "hidden lg:flex sticky top-14 z-20 h-[calc(100vh-3.5rem)] shrink-0 flex-col border-r border-subtle bg-surface/40 backdrop-blur-sm transition-[width] duration-200",
        collapsed ? "w-[60px]" : "w-[240px]"
      )}
      aria-label="Primary navigation"
      data-collapsed={collapsed ? "true" : "false"}
    >
      <div
        className={cn(
          "flex items-center border-b border-subtle px-2 py-2",
          collapsed ? "justify-center" : "justify-between"
        )}
      >
        {!collapsed && (
          <span className="px-2 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
            Navigation
          </span>
        )}
        <button
          type="button"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          onClick={toggle}
          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-ink-tertiary transition hover:bg-white/[0.05] hover:text-ink-primary"
        >
          {collapsed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}
        </button>
      </div>

      <nav className="flex-1 overflow-y-auto overflow-x-hidden py-2">
        {sections.map((section, idx) => (
          <div
            key={section.label ?? `g-${idx}`}
            className={cn(idx > 0 && "mt-3")}
          >
            {section.label && !collapsed ? (
              <p className="mb-1 px-3 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                {section.label}
              </p>
            ) : null}
            {section.label && collapsed ? (
              <div className="mx-2 my-1.5 border-t border-subtle" aria-hidden />
            ) : null}
            <ul className="flex flex-col gap-0.5 px-2">
              {section.items.map((item) => {
                const active =
                  pathname === item.href ||
                  pathname?.startsWith(item.href + "/");
                const Icon = item.icon;
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      title={collapsed ? item.label : undefined}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "group relative flex items-center gap-2.5 rounded-md text-sm transition",
                        collapsed ? "justify-center px-2 py-2" : "px-2.5 py-2",
                        active
                          ? "bg-brand-500/10 text-brand-300"
                          : "text-ink-secondary hover:bg-white/[0.04] hover:text-ink-primary"
                      )}
                    >
                      {active && (
                        <span
                          aria-hidden
                          className="absolute inset-y-1 left-0 w-[2px] rounded-r bg-brand-500"
                        />
                      )}
                      <Icon
                        size={16}
                        className={cn(
                          "shrink-0",
                          active ? "text-brand-300" : "text-ink-tertiary group-hover:text-ink-primary"
                        )}
                      />
                      {!collapsed && (
                        <span className="truncate">{item.label}</span>
                      )}
                      {collapsed && (
                        // Tooltip on hover for collapsed state
                        <span
                          role="tooltip"
                          className="pointer-events-none invisible absolute left-full top-1/2 z-50 ml-2 -translate-y-1/2 whitespace-nowrap rounded-md border border-default bg-elevated px-2 py-1 text-xs text-ink-primary shadow-card opacity-0 transition group-hover:visible group-hover:opacity-100"
                        >
                          {item.label}
                        </span>
                      )}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      {!collapsed && (
        <div className="border-t border-subtle px-3 py-2">
          <p className="font-mono text-[10px] text-ink-tertiary">
            v0.1 dev preview
          </p>
        </div>
      )}
    </aside>
  );
}

// Mobile drawer-friendly version of the same nav, rendered inside the header
// drawer for screens below the lg breakpoint.
export function SidebarMobileNav({
  canAdmin = false,
  onNavigate,
}: {
  canAdmin?: boolean;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();
  const sections = NAV_SECTIONS.filter((s) => !s.adminOnly || canAdmin);

  return (
    <nav className="flex flex-col gap-1" aria-label="Primary navigation">
      {sections.map((section, idx) => (
        <div key={section.label ?? `g-${idx}`}>
          {section.label ? (
            <p className="px-3 pb-1 pt-3 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
              {section.label}
            </p>
          ) : null}
          {section.items.map((item) => {
            const active =
              pathname === item.href ||
              pathname?.startsWith(item.href + "/");
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                onClick={onNavigate}
                className={cn(
                  "flex items-center gap-2.5 rounded-md px-3 py-2.5 text-sm transition",
                  active
                    ? "bg-brand-500/10 text-brand-300"
                    : "text-ink-secondary hover:bg-white/[0.04] hover:text-ink-primary"
                )}
              >
                <Icon
                  size={16}
                  className={cn(
                    "shrink-0",
                    active ? "text-brand-300" : "text-ink-tertiary"
                  )}
                />
                <span className="flex-1">{item.label}</span>
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronsLeft, ChevronsRight } from "lucide-react";
import { cn } from "@/lib/cn";
import { visibleSections, type NavSection } from "@/lib/nav-sections";
import { APP_VERSION_LABEL } from "@/lib/version";
import { LegalFooterLinks } from "@/components/legal-footer-links";

const COOKIE = "aops_sidebar_collapsed";
const SECTIONS_KEY = "aops_nav_sections_open";

function readCollapsedCookie(): boolean {
  if (typeof document === "undefined") return false;
  return document.cookie.split("; ").some((c) => c.startsWith(`${COOKIE}=1`));
}
function writeCollapsedCookie(collapsed: boolean) {
  document.cookie = `${COOKIE}=${collapsed ? "1" : "0"}; Path=/; SameSite=Lax; Max-Age=31536000`;
}

function isActive(pathname: string | null, href: string): boolean {
  return pathname === href || (pathname?.startsWith(href + "/") ?? false);
}

export function AppSidebar({
  canAdmin = false,
  initialCollapsed = false,
  workspaceName = "Workspace",
  isCommunity = false,
}: {
  canAdmin?: boolean;
  initialCollapsed?: boolean;
  workspaceName?: string;
  isCommunity?: boolean;
}) {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(initialCollapsed);
  const sections = useMemo(
    () => visibleSections(canAdmin, isCommunity),
    [canAdmin, isCommunity]
  );

  // Section open/closed state — server-safe default from defaultOpen, then
  // hydrate user prefs from localStorage and force-open the active section.
  const [open, setOpen] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(sections.map((s) => [s.label ?? "", s.defaultOpen ?? true]))
  );

  useEffect(() => {
    setCollapsed(readCollapsedCookie());
    let prefs: Record<string, boolean> = {};
    try {
      prefs = JSON.parse(localStorage.getItem(SECTIONS_KEY) ?? "{}");
    } catch {
      /* ignore */
    }
    setOpen((prev) => {
      const next = { ...prev, ...prefs };
      // Always reveal the section containing the active route.
      for (const s of sections) {
        if (s.label && s.items.some((i) => isActive(pathname, i.href))) next[s.label] = true;
      }
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function toggleCollapse() {
    const next = !collapsed;
    setCollapsed(next);
    writeCollapsedCookie(next);
  }

  function toggleSection(label: string) {
    setOpen((prev) => {
      const next = { ...prev, [label]: !prev[label] };
      try {
        localStorage.setItem(SECTIONS_KEY, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  }

  return (
    <aside
      className={cn(
        "hidden lg:flex sticky top-0 z-20 h-screen shrink-0 flex-col border-r border-[var(--hairline)] transition-[width] duration-200",
        "bg-gradient-to-b from-[var(--canvas-2)] to-[var(--canvas)]",
        collapsed ? "w-[60px]" : "w-[240px]"
      )}
      aria-label="Primary navigation"
      data-collapsed={collapsed ? "true" : "false"}
    >
      {/* Brand header */}
      <div className="flex items-center gap-2.5 px-2 py-3.5">
        <div
          className="flex h-[31px] w-[31px] shrink-0 items-center justify-center rounded-[9px] text-[15px] font-bold text-white"
          style={{
            background: "linear-gradient(135deg, var(--brand-400), var(--brand-600) 55%, var(--agent-500))",
            boxShadow: "var(--glow-brand)",
          }}
        >
          ◇
        </div>
        {!collapsed && (
          <div className="min-w-0">
            <div className="text-[14px] font-semibold tracking-tight">Accounting-Ops</div>
            <div className="text-[11px] font-medium text-[var(--ink-3)]" style={{ fontWeight: 450 }}>
              Living Ledger
            </div>
          </div>
        )}
        <button
          type="button"
          onClick={toggleCollapse}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="ml-auto hidden lg:grid h-6 w-6 place-items-center rounded-md text-[var(--ink-4)] hover:text-[var(--ink-1)] hover:bg-[var(--chip-bg)]"
        >
          {collapsed ? <ChevronsRight size={15} /> : <ChevronsLeft size={15} />}
        </button>
      </div>

      {/* Workspace selector — REAL workspace name */}
      <div
        className={cn(
          "flex items-center gap-2 rounded-[11px] border border-[var(--hairline)] bg-[var(--surface)] px-2 py-2 text-[13px] font-medium cursor-pointer transition hover:border-[var(--hairline-strong)] hover:bg-[var(--surface-2)]",
          collapsed ? "justify-center mx-1.5" : "mx-2"
        )}
        title={workspaceName}
      >
        <div
          className="flex h-[23px] w-[23px] shrink-0 items-center justify-center rounded-[7px] text-[11px] font-bold text-white"
          style={{ background: "linear-gradient(135deg, var(--brand-500), var(--brand-700))" }}
        >
          {workspaceName.charAt(0).toUpperCase()}
        </div>
        {!collapsed && <span className="flex-1 truncate tracking-tight">{workspaceName}</span>}
        {!collapsed && (
          <svg className="text-[var(--ink-3)]" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="m6 9 6 6 6-6" />
          </svg>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto overflow-x-hidden py-1">
        {sections.map((section, idx) => {
          const label = section.label ?? `g-${idx}`;
          const isOpen = collapsed ? true : open[label] ?? section.defaultOpen ?? true;
          const regionId = `nav-sec-${idx}`;
          return (
            <div key={label} className={cn(idx > 0 && "mt-1.5")}>
              {section.label && !collapsed ? (
                <button
                  type="button"
                  onClick={() => toggleSection(label)}
                  aria-expanded={isOpen}
                  aria-controls={regionId}
                  className="group flex w-full items-center gap-1 px-3 py-1 text-[10px] font-semibold uppercase tracking-[.13em] text-[var(--ink-4)] hover:text-[var(--ink-2)]"
                >
                  <span className="flex-1 text-left">{section.label}</span>
                  <ChevronDown
                    size={13}
                    className={cn("transition-transform duration-200", isOpen ? "" : "-rotate-90")}
                  />
                </button>
              ) : section.label && collapsed ? (
                <div className="mx-2 my-1.5 border-t border-[var(--hairline)]" aria-hidden />
              ) : null}
              {isOpen && (
                <ul id={regionId} className="flex flex-col gap-0.5 px-1.5 pb-0.5">
                  {section.items.map((item) => {
                    const active = isActive(pathname, item.href);
                    const Icon = item.icon;
                    return (
                      <li key={item.href}>
                        <Link
                          href={item.href}
                          title={collapsed ? item.label : undefined}
                          aria-current={active ? "page" : undefined}
                          className={cn(
                            "group relative flex items-center gap-2.5 rounded-[10px] text-[13px] font-medium tracking-tight transition-[background,color] duration-150",
                            collapsed ? "justify-center px-2 py-2" : "px-2.5 py-1.5",
                            active
                              ? "text-[var(--ink-1)] font-semibold"
                              : "text-[var(--ink-2)] hover:text-[var(--ink-1)] hover:bg-[var(--chip-bg)]"
                          )}
                          style={active ? { background: `linear-gradient(90deg, hsl(${item.hue} / 0.16), transparent)` } : undefined}
                        >
                          {active && (
                            <span
                              aria-hidden
                              className="absolute left-0 top-[6px] bottom-[6px] w-[3px] rounded-r-[3px]"
                              style={{ background: `hsl(${item.hue})`, boxShadow: `0 0 10px hsl(${item.hue} / 0.8)` }}
                            />
                          )}
                          <span
                            className="flex h-[27px] w-[27px] shrink-0 items-center justify-center rounded-[8px] border transition-[background,color,box-shadow] duration-150"
                            style={{
                              background: active ? `hsl(${item.hue})` : `hsl(${item.hue} / 0.14)`,
                              color: active ? "#fff" : `hsl(${item.hue})`,
                              borderColor: active ? "transparent" : `hsl(${item.hue} / 0.22)`,
                              boxShadow: active ? `0 0 16px hsl(${item.hue} / 0.55)` : undefined,
                            }}
                          >
                            <Icon size={15} />
                          </span>
                          {!collapsed && <span className="truncate">{item.label}</span>}
                          {collapsed && (
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
              )}
            </div>
          );
        })}
      </nav>

      {!collapsed && (
        <div className="border-t border-[var(--hairline)] px-3 py-2.5 space-y-1.5">
          <div className="flex items-center gap-2 text-[11px] text-[var(--ink-4)]">
            <span className="dot-live" />
            <span>{APP_VERSION_LABEL}</span>
          </div>
          <LegalFooterLinks className="text-[var(--ink-4)] [&_a]:text-[var(--ink-4)] [&_a:hover]:text-[var(--ink-2)]" />
        </div>
      )}
    </aside>
  );
}

export function SidebarMobileNav({
  canAdmin = false,
  onNavigate,
  isCommunity = false,
}: {
  canAdmin?: boolean;
  onNavigate?: () => void;
  isCommunity?: boolean;
}) {
  const pathname = usePathname();
  const sections: NavSection[] = visibleSections(canAdmin, isCommunity);

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
            const active = isActive(pathname, item.href);
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                onClick={onNavigate}
                className={cn(
                  "flex items-center gap-2.5 rounded-md px-3 py-2.5 text-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas",
                  active
                    ? "bg-brand-500/10 text-brand-300"
                    : "text-ink-secondary hover:bg-white/[0.04] hover:text-ink-primary"
                )}
              >
                <span
                  className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-[7px] border"
                  style={{
                    background: active ? `hsl(${item.hue})` : `hsl(${item.hue} / 0.14)`,
                    color: active ? "#fff" : `hsl(${item.hue})`,
                    borderColor: active ? "transparent" : `hsl(${item.hue} / 0.22)`,
                  }}
                >
                  <Icon size={14} />
                </span>
                <span className="flex-1">{item.label}</span>
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

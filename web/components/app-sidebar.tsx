"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { ChevronsLeft, ChevronsRight } from "lucide-react";
import { cn } from "@/lib/cn";
import { NAV_SECTIONS } from "@/lib/nav-sections";
import { APP_VERSION_LABEL } from "@/lib/version";

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
        "hidden lg:flex sticky top-0 z-20 h-screen shrink-0 flex-col border-r border-subtle transition-[width] duration-200",
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
            boxShadow: "var(--glow-brand)"
          }}
        >
          ◇
        </div>
        {!collapsed && (
          <div>
            <div className="text-[14px] font-semibold tracking-tight">Accounting-Ops</div>
            <div className="text-[11px] font-medium text-[var(--ink-3)]" style={{ fontWeight: 450 }}>Living Ledger</div>
          </div>
        )}
      </div>

      {/* Workspace selector */}
      <div
        className={cn(
          "mx-1 flex items-center gap-2 rounded-[11px] border border-[var(--hairline)] bg-[var(--surface)] px-2 py-2 text-[13px] font-medium cursor-pointer transition hover:border-[var(--hairline-strong)] hover:bg-[var(--surface-2)]",
          collapsed ? "justify-center mx-1.5" : "mx-2"
        )}
      >
        <div
          className="flex h-[23px] w-[23px] shrink-0 items-center justify-center rounded-[7px] text-[11px] font-bold text-white"
          style={{
            background: "linear-gradient(135deg, var(--brand-500), var(--brand-700))"
          }}
        >
          M
        </div>
        {!collapsed && <span className="flex-1 truncate tracking-tight">Magic Unicorn Inc</span>}
        {!collapsed && (
          <svg className="text-[var(--ink-3)]" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="m6 9 6 6 6-6" />
          </svg>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto overflow-x-hidden py-1">
        {sections.map((section, idx) => (
          <div key={section.label ?? `g-${idx}`} className={cn(idx > 0 && "mt-2")}>
            {section.label && !collapsed ? (
              <p className="mb-1 px-3 text-[10px] font-semibold uppercase tracking-[.13em] text-[var(--ink-4)]">
                {section.label}
              </p>
            ) : null}
            {section.label && collapsed ? (
              <div className="mx-2 my-1.5 border-t border-[var(--hairline)]" aria-hidden />
            ) : null}
            <ul className="flex flex-col gap-0.5 px-1.5">
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
                        "group relative flex items-center gap-2.5 rounded-[10px] text-[13px] font-medium tracking-tight transition-[background,color] duration-150",
                        collapsed ? "justify-center px-2 py-2" : "px-2.5 py-1.5",
                        active
                          ? "text-[var(--ink-1)] font-semibold"
                          : "text-[var(--ink-2)] hover:text-[var(--ink-1)] hover:bg-[var(--chip-bg)]"
                      )}
                      style={active ? {
                        background: `linear-gradient(90deg, hsl(${item.hue} / 0.16), transparent)`
                      } : undefined}
                    >
                      {/* Left accent bar for active item */}
                      {active && (
                        <span
                          aria-hidden
                          className="absolute left-0 top-[6px] bottom-[6px] w-[3px] rounded-r-[3px]"
                          style={{
                            background: `hsl(${item.hue})`,
                            boxShadow: `0 0 10px hsl(${item.hue} / 0.8)`
                          }}
                        />
                      )}
                      {/* Colored icon chip */}
                      <span
                        className={cn(
                          "flex h-[27px] w-[27px] shrink-0 items-center justify-center rounded-[8px] border transition-[background,color,box-shadow] duration-150",
                          collapsed ? "" : ""
                        )}
                        style={{
                          background: active
                            ? `hsl(${item.hue})`
                            : `hsl(${item.hue} / 0.14)`,
                          color: active ? "#fff" : `hsl(${item.hue})`,
                          borderColor: active
                            ? "transparent"
                            : `hsl(${item.hue} / 0.22)`,
                          boxShadow: active
                            ? `0 0 16px hsl(${item.hue} / 0.55)`
                            : undefined,
                        }}
                        onMouseEnter={(e) => {
                          if (!active) {
                            e.currentTarget.style.background = `hsl(${item.hue} / 0.24)`;
                            e.currentTarget.style.boxShadow = `0 0 14px hsl(${item.hue} / 0.25)`;
                          }
                        }}
                        onMouseLeave={(e) => {
                          if (!active) {
                            e.currentTarget.style.background = `hsl(${item.hue} / 0.14)`;
                            e.currentTarget.style.boxShadow = "none";
                          }
                        }}
                      >
                        <Icon size={15} />
                      </span>
                      {!collapsed && (
                        <span className="truncate">{item.label}</span>
                      )}
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
          </div>
        ))}
      </nav>

      {!collapsed && (
        <div className="flex items-center gap-2 border-t border-[var(--hairline)] px-3 py-2.5 text-[11px] text-[var(--ink-4)]">
          <span className="dot-live" />
          <span>{APP_VERSION_LABEL}</span>
        </div>
      )}
    </aside>
  );
}

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
                  "flex items-center gap-2.5 rounded-md px-3 py-2.5 text-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas",
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

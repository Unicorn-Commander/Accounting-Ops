"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Menu, X } from "lucide-react";
import { cn } from "@/lib/cn";

const NAV_SECTIONS = [
  {
    label: null,
    items: [
      ["Dashboard", "/dashboard"],
      ["Transactions", "/transactions"],
      ["Reconciliation", "/reconciliation-queue"],
      ["Approvals", "/approvals"],
      ["Agent activity", "/agent-activity"],
      ["Journal", "/journal"],
      ["Chart of Accounts", "/coa"],
    ],
  },
  {
    label: "Reports",
    items: [
      ["Reports", "/reports"],
      ["Trial Balance", "/reports/trial-balance"],
      ["Taxes", "/taxes"],
      ["Knowledge", "/knowledge"],
      ["CPA Export", "/cpa-export"],
    ],
  },
  {
    label: "Admin",
    adminOnly: true,
    items: [["Period Close", "/admin/period-close"]],
  },
  {
    label: null,
    items: [
      ["Connectors", "/connectors"],
      ["Help", "/help"],
    ],
  },
];

export function SiteHeaderClient({
  variant,
  userBlock,
  children,
  canAdmin = false
}: {
  variant: "app" | "marketing";
  userBlock: React.ReactNode;
  children?: React.ReactNode;
  canAdmin?: boolean;
}) {
  const pathname = usePathname();
  const [mobileOpen, setMobileOpen] = useState(false);

  // Close mobile drawer when route changes
  useEffect(() => {
    setMobileOpen(false);
  }, [pathname]);

  // Lock body scroll when drawer open
  useEffect(() => {
    if (mobileOpen) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [mobileOpen]);

  return (
    <>
      <header
        className={cn(
          "sticky top-0 z-30 w-full border-b border-subtle backdrop-blur-md",
          variant === "marketing" ? "bg-canvas/60" : "bg-canvas/85"
        )}
      >
        <div className="mx-auto flex max-w-7xl items-center gap-2 px-4 py-3 md:gap-4 md:px-6 md:py-3.5">
          {/* Mobile hamburger (app variant only) */}
          {variant === "app" && (
            <button
              type="button"
              aria-label="Open menu"
              onClick={() => setMobileOpen(true)}
              className="lg:hidden -ml-1 inline-flex h-9 w-9 items-center justify-center rounded-md text-ink-secondary hover:bg-white/[0.05] hover:text-ink-primary"
            >
              <Menu size={18} />
            </button>
          )}

          <Link
            href={variant === "marketing" ? "/" : "/dashboard"}
            className="flex items-center gap-2 text-[15px] font-semibold tracking-tight"
          >
            <Logo />
            <span className="hidden sm:inline">Accounting-Ops</span>
            <span className="hidden lg:inline-block text-[10px] font-medium uppercase tracking-wider text-ink-tertiary border border-subtle rounded-full px-2 py-0.5">
              v0.1 dev preview
            </span>
          </Link>

          {variant === "app" && (
            <nav className="ml-6 hidden flex-1 items-center gap-1 lg:flex">
              {NAV_SECTIONS.filter((section) => !section.adminOnly || canAdmin).map(
                (section) => (
                  <div key={section.label ?? section.items.map(([, href]) => href).join("|")} className="flex items-center gap-1">
                    {section.label ? (
                      <span className="px-1.5 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                        {section.label}
                      </span>
                    ) : null}
                    {section.items.map(([label, href]) => {
                      const active =
                        pathname === href || pathname?.startsWith(href + "/");
                      return (
                        <Link
                          key={href}
                          href={href}
                          className={cn(
                            "rounded-md px-2.5 py-1.5 text-sm transition",
                            active
                              ? "bg-white/5 text-ink-primary"
                              : "text-ink-secondary hover:text-ink-primary hover:bg-white/[0.03]"
                          )}
                        >
                          {label}
                        </Link>
                      );
                    })}
                  </div>
                )
              )}
            </nav>
          )}

          <div className="ml-auto flex items-center gap-2 md:gap-3">
            {variant === "marketing" ? (
              <>
                <Link
                  href="/login"
                  className="hidden sm:inline-block text-sm text-ink-secondary hover:text-ink-primary"
                >
                  Sign in
                </Link>
                <a
                  href="mailto:aaron@magicunicorn.tech?subject=Accounting-Ops dev preview"
                  className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-sm font-medium text-white shadow-brand-glow hover:bg-brand-600 transition"
                >
                  Request access
                </a>
              </>
            ) : (
              <>
                <div className="hidden xl:flex items-center gap-2 text-xs text-ink-tertiary">
                  <span className="h-1.5 w-1.5 rounded-full bg-credit shadow-[0_0_6px_hsl(157_64%_52%_/_0.8)]" />
                  <span>All agents online</span>
                </div>
                {/* Workspace/Entity selectors — hide on small screens since drawer has them */}
                <div className="hidden md:flex items-center gap-2">{children}</div>
                {userBlock}
              </>
            )}
          </div>
        </div>
      </header>

      {/* Mobile drawer */}
      {variant === "app" && mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          {/* Backdrop */}
          <button
            type="button"
            aria-label="Close menu"
            onClick={() => setMobileOpen(false)}
            className="absolute inset-0 bg-canvas/85 backdrop-blur-sm"
          />
          {/* Drawer */}
          <div className="relative ml-auto h-full w-[88%] max-w-xs overflow-y-auto border-l border-subtle bg-surface px-5 py-5 shadow-card">
            <div className="mb-4 flex items-center justify-between">
              <span className="text-sm font-semibold">Menu</span>
              <button
                type="button"
                aria-label="Close menu"
                onClick={() => setMobileOpen(false)}
                className="inline-flex h-9 w-9 items-center justify-center rounded-md text-ink-secondary hover:bg-white/[0.05]"
              >
                <X size={18} />
              </button>
            </div>

            {/* Workspace + Entity at the top of the drawer (mobile-only place) */}
            {children && (
              <div className="mb-5 flex flex-col gap-2 border-b border-subtle pb-5">
                {children}
              </div>
            )}

            <nav className="flex flex-col gap-1">
              {NAV_SECTIONS.filter((section) => !section.adminOnly || canAdmin).map(
                (section) => (
                  <div key={section.label ?? section.items.map(([, href]) => href).join("|")}>
                    {section.label ? (
                      <p className="px-3 pb-1 pt-3 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                        {section.label}
                      </p>
                    ) : null}
                    {section.items.map(([label, href]) => {
                      const active =
                        pathname === href || pathname?.startsWith(href + "/");
                      return (
                        <Link
                          key={href}
                          href={href}
                          className={cn(
                            "block rounded-md px-3 py-2.5 text-sm transition",
                            active
                              ? "bg-brand-500/10 text-brand-300"
                              : "text-ink-secondary hover:bg-white/[0.04] hover:text-ink-primary"
                          )}
                        >
                          {label}
                        </Link>
                      );
                    })}
                  </div>
                )
              )}
            </nav>
          </div>
        </div>
      )}
    </>
  );
}

function Logo() {
  return (
    <svg
      width="22"
      height="22"
      viewBox="0 0 22 22"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <linearGradient
          id="ulg"
          x1="0"
          y1="0"
          x2="22"
          y2="22"
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor="hsl(265 78% 62%)" />
          <stop offset="1" stopColor="hsl(188 95% 56%)" />
        </linearGradient>
      </defs>
      <rect x="1" y="1" width="20" height="20" rx="5" fill="url(#ulg)" opacity="0.18" />
      <rect
        x="1.5"
        y="1.5"
        width="19"
        height="19"
        rx="4.5"
        stroke="url(#ulg)"
        strokeWidth="1"
      />
      <path
        d="M6 14V8L11 11L16 8V14"
        stroke="url(#ulg)"
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  );
}

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Menu, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { SidebarMobileNav } from "@/components/app-sidebar";

// Marketing-only nav lives here; the app variant uses the persistent
// AppSidebar for primary navigation.
const MARKETING_NAV: Array<[string, string]> = [
  ["Sign in", "/login"],
];

export function SiteHeaderClient({
  variant,
  userBlock,
  children,
  canAdmin = false,
}: {
  variant: "app" | "marketing";
  userBlock: React.ReactNode;
  children?: React.ReactNode;
  canAdmin?: boolean;
}) {
  const pathname = usePathname();
  const [mobileOpen, setMobileOpen] = useState(false);

  // Close mobile drawer on route change
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
        <div
          className={cn(
            "flex h-14 items-center gap-2 px-4 md:gap-4 md:px-5",
            // Marketing header centers in 7xl; app header spans full so the
            // chips sit at the right edge above the right edge of content.
            variant === "marketing" && "mx-auto max-w-7xl"
          )}
        >
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

          <div className="ml-auto flex items-center gap-2 md:gap-3">
            {variant === "marketing" ? (
              <>
                {MARKETING_NAV.map(([label, href]) => (
                  <Link
                    key={href}
                    href={href}
                    className="hidden sm:inline-block text-sm text-ink-secondary hover:text-ink-primary"
                  >
                    {label}
                  </Link>
                ))}
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
                {/* Workspace + Entity chips — full version on md+ */}
                <div className="hidden md:flex items-center gap-2">{children}</div>
                {userBlock}
              </>
            )}
          </div>
        </div>
      </header>

      {/* Mobile drawer (app variant only) */}
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
          <div className="relative h-full w-[88%] max-w-xs overflow-y-auto border-r border-subtle bg-surface px-4 py-5 shadow-card">
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

            {/* Workspace + Entity at top */}
            {children && (
              <div className="mb-5 flex flex-col gap-2 border-b border-subtle pb-5">
                {children}
              </div>
            )}

            <SidebarMobileNav canAdmin={canAdmin} onNavigate={() => setMobileOpen(false)} />
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

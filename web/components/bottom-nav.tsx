"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { MoreHorizontal, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { MOBILE_TABS } from "@/lib/nav-sections";
import { SidebarMobileNav } from "@/components/app-sidebar";

function isActive(pathname: string | null, href: string): boolean {
  return pathname === href || (pathname?.startsWith(href + "/") ?? false);
}

// Phone-width bottom tab bar (4 destinations + More). Hidden at lg+ where the
// sidebar takes over. Respects the iOS/Android safe-area inset and hides while
// a text field is focused (the keyboard would otherwise float it mid-screen).
export function BottomNav({
  canAdmin = false,
  isCommunity = false,
}: {
  canAdmin?: boolean;
  isCommunity?: boolean;
}) {
  const pathname = usePathname();
  const [moreOpen, setMoreOpen] = useState(false);
  const [keyboardOpen, setKeyboardOpen] = useState(false);

  useEffect(() => {
    function onFocus(e: FocusEvent) {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) {
        setKeyboardOpen(true);
      }
    }
    function onBlur() {
      setKeyboardOpen(false);
    }
    document.addEventListener("focusin", onFocus);
    document.addEventListener("focusout", onBlur);
    return () => {
      document.removeEventListener("focusin", onFocus);
      document.removeEventListener("focusout", onBlur);
    };
  }, []);

  // Close the More sheet on navigation.
  useEffect(() => {
    setMoreOpen(false);
  }, [pathname]);

  const moreActive =
    moreOpen || !MOBILE_TABS.some((t) => isActive(pathname, t.href));

  if (keyboardOpen) return null;

  return (
    <>
      <nav
        aria-label="Primary"
        className="fixed inset-x-0 bottom-0 z-40 flex lg:hidden border-t border-[var(--hairline)] bg-[var(--canvas)]/95 backdrop-blur-lg"
        style={{ paddingBottom: "env(safe-area-inset-bottom, 0px)" }}
      >
        {MOBILE_TABS.map((tab) => {
          const active = isActive(pathname, tab.href);
          const Icon = tab.icon;
          return (
            <Link
              key={tab.href}
              href={tab.href}
              aria-current={active ? "page" : undefined}
              className="flex flex-1 flex-col items-center justify-center gap-0.5 py-2 min-h-[48px]"
              style={{ color: active ? `hsl(${tab.hue})` : "var(--ink-3)" }}
            >
              <Icon size={20} strokeWidth={active ? 2.4 : 1.9} />
              <span className="text-[10px] font-medium tracking-tight">{tab.label}</span>
            </Link>
          );
        })}
        <button
          type="button"
          onClick={() => setMoreOpen(true)}
          aria-label="More"
          aria-expanded={moreOpen}
          className="flex flex-1 flex-col items-center justify-center gap-0.5 py-2 min-h-[48px]"
          style={{ color: moreActive ? "var(--ink-1)" : "var(--ink-3)" }}
        >
          <MoreHorizontal size={20} strokeWidth={moreActive ? 2.4 : 1.9} />
          <span className="text-[10px] font-medium tracking-tight">More</span>
        </button>
      </nav>

      {/* More sheet — the full grouped nav */}
      {moreOpen && (
        <div className="fixed inset-0 z-50 lg:hidden" role="dialog" aria-modal="true" aria-label="All navigation">
          <button
            type="button"
            aria-label="Close"
            onClick={() => setMoreOpen(false)}
            className="absolute inset-0 bg-[var(--scrim)] backdrop-blur-sm"
          />
          <div
            className="absolute inset-x-0 bottom-0 max-h-[80vh] overflow-y-auto rounded-t-2xl border-t border-[var(--hairline-strong)] bg-[var(--canvas-2)] p-3"
            style={{ paddingBottom: "calc(env(safe-area-inset-bottom, 0px) + 12px)" }}
          >
            <div className="mb-1 flex items-center justify-between px-2">
              <span className="text-[13px] font-semibold">All sections</span>
              <button
                type="button"
                onClick={() => setMoreOpen(false)}
                aria-label="Close"
                className="grid h-8 w-8 place-items-center rounded-lg text-[var(--ink-3)] hover:bg-[var(--chip-bg)] hover:text-[var(--ink-1)]"
              >
                <X size={16} />
              </button>
            </div>
            <SidebarMobileNav canAdmin={canAdmin} isCommunity={isCommunity} onNavigate={() => setMoreOpen(false)} />
          </div>
        </div>
      )}
    </>
  );
}

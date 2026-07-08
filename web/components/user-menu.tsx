"use client";

import { useState } from "react";
import { ChevronDown, LogOut, User as UserIcon } from "lucide-react";
import { cn } from "@/lib/cn";

export function UserMenu({
  uc_uid,
  displayName,
  email
}: {
  uc_uid?: string;
  displayName?: string;
  email?: string;
}) {
  const [open, setOpen] = useState(false);
  const label = displayName ?? uc_uid ?? "Signed in";

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 rounded-md border border-default bg-surface px-2.5 py-1.5 text-xs transition hover:bg-elevated"
      >
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-brand-500/20 text-brand-400 ring-1 ring-inset ring-brand-500/40">
          <UserIcon size={11} />
        </span>
        <span className="hidden font-medium md:inline">{label}</span>
        <ChevronDown
          size={11}
          className={cn(
            "text-ink-tertiary transition-transform",
            open && "rotate-180"
          )}
        />
      </button>

      {open && (
        <div className="absolute right-0 z-40 mt-1.5 w-64 rounded-lg border border-default bg-elevated shadow-card">
          <div className="border-b border-subtle p-3">
            <p className="text-sm font-semibold">{label}</p>
            {email && (
              <p className="mt-0.5 truncate text-[11px] text-ink-tertiary">{email}</p>
            )}
            {uc_uid && (
              <p className="mt-0.5 font-mono text-[10px] text-ink-disabled">
                uc_uid: {uc_uid}
              </p>
            )}
          </div>
          <div className="p-1">
            <form action="/api/auth/signout" method="post">
              <button
                type="submit"
                className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-sm text-ink-secondary hover:bg-white/[0.03] hover:text-ink-primary"
              >
                <LogOut size={13} />
                Sign out
              </button>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

"use client";

import { useRouter } from "next/navigation";
import { ArrowRight, ShieldCheck } from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import type { ClientsDashboard } from "@/lib/api";

export function ClientsTable({
  persona,
  workspaces,
}: {
  persona: ClientsDashboard["persona"];
  workspaces: ClientsDashboard["workspaces"];
}) {
  const router = useRouter();

  function gotoWorkspace(id: string) {
    document.cookie = `aops_workspace=${id}; Path=/; SameSite=Lax; Max-Age=2592000`;
    document.cookie = `aops_entity=; Path=/; SameSite=Lax; Max-Age=0`;
    router.push("/dashboard");
  }

  return (
    <Card padded={false}>
      <div className="flex items-center justify-between border-b border-subtle px-4 py-3">
        <h2 className="text-sm font-semibold">
          {persona === "cpa" ? "Assigned workspaces" : "Your workspaces"} ({workspaces.length})
        </h2>
        {persona === "cpa" ? (
          <span className="text-[10px] uppercase tracking-wider text-pending">
            CPA persona
          </span>
        ) : null}
      </div>

      {workspaces.length === 0 ? (
        <p className="p-4 text-sm text-ink-tertiary">No workspaces.</p>
      ) : (
        <ul className="divide-y divide-subtle">
          {workspaces.map((w) => (
            <li
              key={w.id}
              className="group flex flex-col gap-2 px-4 py-3 transition hover:bg-white/[0.02] md:flex-row md:items-center md:gap-4"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate font-medium">{w.display_name}</span>
                  {w.hipaa_protected ? (
                    <ShieldCheck
                      size={11}
                      className="text-agent-500"
                      aria-label="HIPAA-protected"
                    />
                  ) : null}
                  <span
                    className={cn(
                      "rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider",
                      w.role === "cpa" || w.role === "accountant"
                        ? "border-pending/40 bg-pending/15 text-pending"
                        : w.role === "owner" || w.role === "client_owner"
                          ? "border-brand-500/40 bg-brand-500/15 text-brand-300"
                          : w.role === "admin"
                            ? "border-agent-500/30 bg-agent-500/15 text-agent-500"
                            : "border-subtle bg-white/5 text-ink-tertiary"
                    )}
                  >
                    {w.role}
                  </span>
                </div>
                <div className="mt-0.5 text-xs text-ink-tertiary">
                  {w.entity_count} entit{w.entity_count === 1 ? "y" : "ies"}
                  {" · "}
                  {w.entity_scope && w.entity_scope.length > 0
                    ? `restricted to ${w.entity_scope.length}`
                    : "all entities"}
                  {" · "}slug <code>{w.slug}</code>
                </div>
              </div>

              <div className="flex items-center gap-3">
                <div className="text-right">
                  <p
                    className={cn(
                      "font-mono text-numeric-md",
                      w.open_items_count > 0 ? "text-pending" : "text-ink-tertiary"
                    )}
                  >
                    {w.open_items_count}
                  </p>
                  <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                    open
                  </p>
                </div>
                <div className="text-right text-[11px] text-ink-tertiary">
                  {w.last_active_at ? (
                    <>Last visited<br />{new Date(w.last_active_at).toLocaleDateString()}</>
                  ) : (
                    "Never visited"
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => gotoWorkspace(w.id)}
                  className="inline-flex items-center gap-1 rounded-md bg-brand-500/15 px-3 py-1.5 text-sm font-medium text-brand-300 transition hover:bg-brand-500/25"
                >
                  Switch <ArrowRight size={12} />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

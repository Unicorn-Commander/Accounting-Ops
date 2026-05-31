"use client";

// Recent CPA activity row — client component so it can switch the workspace
// cookie + push the user to the activity's deep-link path in one click.

import { useRouter } from "next/navigation";
import { ArrowRight } from "lucide-react";
import type { CpaRecentActivityItem } from "@/lib/api";

export function RecentActivityRow({ item }: { item: CpaRecentActivityItem }) {
  const router = useRouter();
  // Backend always returns a deep_link_path; fall back to /dashboard for old
  // payloads pre-deploy.
  const path = item.deep_link_path ?? "/dashboard";

  function goto() {
    document.cookie = `aops_workspace=${item.workspace_id}; Path=/; SameSite=Lax; Max-Age=2592000`;
    document.cookie = `aops_entity=; Path=/; SameSite=Lax; Max-Age=0`;
    router.push(path);
  }

  return (
    <li className="flex flex-col gap-1 py-3 md:flex-row md:items-center md:gap-4">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm">
          <span className="font-medium">{item.tool}</span>{" "}
          <span className="text-ink-tertiary">→</span>{" "}
          <span className="text-ink-secondary">{item.outcome}</span>
        </p>
        <p className="truncate text-xs text-ink-tertiary">
          <button
            type="button"
            onClick={goto}
            className="inline-flex items-center gap-0.5 hover:text-ink-secondary hover:underline focus:outline-none focus-visible:text-ink-secondary"
            aria-label={`Switch to ${item.workspace_name} and open ${path}`}
          >
            {item.workspace_name}
            <ArrowRight size={9} />
            <span className="font-mono text-[10px]">{path}</span>
          </button>{" "}
          · {item.model_version || "anonymous agent"}
        </p>
      </div>
      <span className="text-[11px] text-ink-tertiary">
        {new Date(item.created_at).toLocaleString()}
      </span>
    </li>
  );
}

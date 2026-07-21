import { Briefcase, Building2, ChevronRight, Layers } from "lucide-react";
import { getActiveContext } from "@/lib/active-context";

// Inline hierarchy crumb shown across every app page:
//
//   Workspace · Jane Smith  ›  Entity · Acme Inc  ›  Tax Year · 2024
//
// Each segment is purely informational here (the chips in the header are
// the interactive controls). The crumb just reassures the user about
// what's currently scoping the view.
//
// Pass `taxYear` from /taxes/[year] (etc.) to append a third segment.
export async function ContextBreadcrumb({
  taxYear,
  className,
}: {
  taxYear?: number | string;
  className?: string;
}) {
  const ctx = await getActiveContext();
  if (!ctx.activeWorkspace) return null;

  const entityLabel = ctx.isConsolidated
    ? "All entities (consolidated)"
    : ctx.activeEntity?.display_name ?? "—";

  return (
    <div
      className={
        "flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-ink-tertiary " +
        (className ?? "")
      }
      aria-label="Current scope"
    >
      <span className="inline-flex items-center gap-1.5">
        <Building2 size={11} className="text-ink-disabled" />
        <span className="uppercase tracking-wider">Workspace</span>
        <span className="text-ink-secondary">·</span>
        <span className="font-medium text-ink-secondary">
          {ctx.activeWorkspace.display_name}
        </span>
      </span>

      {ctx.entities.length > 0 && (
        <>
          <ChevronRight size={11} className="text-ink-disabled" aria-hidden />
          <span className="inline-flex items-center gap-1.5">
            {ctx.isConsolidated ? (
              <Layers size={11} className="text-brand-400" />
            ) : (
              <Briefcase size={11} className="text-ink-disabled" />
            )}
            <span className="uppercase tracking-wider">Entity</span>
            <span className="text-ink-secondary">·</span>
            <span className="font-medium text-ink-secondary">{entityLabel}</span>
          </span>
        </>
      )}

      {taxYear !== undefined && (
        <>
          <ChevronRight size={11} className="text-ink-disabled" aria-hidden />
          <span className="inline-flex items-center gap-1.5">
            <span className="uppercase tracking-wider">Tax Year</span>
            <span className="text-ink-secondary">·</span>
            <span className="font-medium text-ink-secondary">{taxYear}</span>
          </span>
        </>
      )}
    </div>
  );
}

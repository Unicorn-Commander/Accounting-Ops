// Server component: entity-kind -> appropriate tax form variant.
//
// /taxes/[year] is the headline tax review surface. Historically it has
// only rendered Form 1120 (C-Corp) and showed a banner for everyone else.
// This router dispatches by entity.kind to the right form variant.
//
// Routing table:
//
//   c_corp        -> Form 1120         (full year view: Narrative / Manual / Specialist / Runs / IRS Forms)
//   s_corp        -> Form 1120-S       (placeholder + chat handoff)
//   llc           -> Form 1065         (placeholder; multi-member LLCs file partnership returns)
//   partnership   -> Form 1065         (placeholder + chat handoff)
//   sole_prop     -> Schedule C         (placeholder + chat handoff)
//   personal      -> Form 1040          (links to personal_1040_specialist drafts)
//   nonprofit     -> Form 990          (placeholder + chat handoff)
//   trust         -> Form 1041          (placeholder)
//   estate        -> Form 1041          (placeholder)
//   unknown       -> Form 1120 default  (banner explaining the fallback)
//
// For now the non-C-Corp variants are clearly-labeled "work-in-progress"
// placeholders that route the user to /chat with the correct specialist
// persona. The C-Corp surface (the headline feature) is unchanged.

import Link from "next/link";
import { ArrowLeft, ExternalLink, MessageSquare, Sparkles } from "lucide-react";
import { Card } from "@/components/primitives";
import { EntitySelector } from "@/components/entity-selector";
import type { Entity, Workspace } from "@/lib/api";
import { fetch1120Package } from "@/lib/taxes-1120";
import { fetchSpecialistRuns } from "@/lib/specialist-runs";
import { fetchTaxReviewQueue } from "@/lib/tax-review-queue";
import { YearView } from "./year-view";
import { canRunDrafts } from "@/lib/tax-run-launch";

// ---------------------------------------------------------------------------
// Form metadata per kind
// ---------------------------------------------------------------------------

type FormVariant = {
  formId: string;
  formName: string;
  // Recommended specialist persona slug in /chat for follow-up. The picker
  // accepts ?persona= so the URL deep-links.
  specialistPersona: string;
  specialistDisplayName: string;
  // Short, plain explanation of what the form does — surfaced as the page
  // subtitle.
  shortBlurb: string;
  // Long blurb used in the placeholder body.
  longBlurb: string;
};

const FORM_BY_KIND: Record<string, FormVariant> = {
  c_corp: {
    formId: "1120",
    formName: "Form 1120",
    specialistPersona: "form-1120-preparer",
    specialistDisplayName: "Form 1120 Preparer",
    shortBlurb: "U.S. Corporation Income Tax Return (CPA review package)",
    longBlurb: "The headline CPA review package for C-Corporations."
  },
  s_corp: {
    formId: "1120-S",
    formName: "Form 1120-S",
    specialistPersona: "cpa-tax-resolver",
    specialistDisplayName: "CPA Tax Resolver",
    shortBlurb: "U.S. Income Tax Return for an S Corporation",
    longBlurb:
      "S-Corps pass income through to shareholders. The 1120-S draft + K-1 generation surface is being built; talk to the CPA Tax Resolver in /chat for now."
  },
  llc: {
    formId: "1065",
    formName: "Form 1065",
    specialistPersona: "cpa-tax-resolver",
    specialistDisplayName: "CPA Tax Resolver",
    shortBlurb: "U.S. Return of Partnership Income (multi-member LLC)",
    longBlurb:
      "Multi-member LLCs default to partnership tax treatment and file Form 1065. The 1065 draft surface is being built; talk to the CPA Tax Resolver in /chat for now."
  },
  partnership: {
    formId: "1065",
    formName: "Form 1065",
    specialistPersona: "cpa-tax-resolver",
    specialistDisplayName: "CPA Tax Resolver",
    shortBlurb: "U.S. Return of Partnership Income",
    longBlurb:
      "Partnerships file Form 1065 + K-1s to partners. The 1065 draft surface is being built; talk to the CPA Tax Resolver in /chat for now."
  },
  sole_prop: {
    formId: "Schedule C",
    formName: "Schedule C",
    specialistPersona: "personal-1040-specialist",
    specialistDisplayName: "Personal 1040 Specialist",
    shortBlurb: "Profit or Loss from Business (sole proprietor)",
    longBlurb:
      "Sole proprietors report business income on Schedule C, attached to the owner's Form 1040. The Schedule C draft surface is being built; talk to the Personal 1040 Specialist in /chat for now."
  },
  personal: {
    formId: "1040",
    formName: "Form 1040",
    specialistPersona: "personal-1040-specialist",
    specialistDisplayName: "Personal 1040 Specialist",
    shortBlurb: "U.S. Individual Income Tax Return",
    longBlurb:
      "Personal entities file Form 1040. The 1040 draft surface routes to the Personal 1040 Specialist; in-app review/edit is being built."
  },
  nonprofit: {
    formId: "990",
    formName: "Form 990",
    specialistPersona: "cpa-tax-resolver",
    specialistDisplayName: "CPA Tax Resolver",
    shortBlurb: "Return of Organization Exempt From Income Tax",
    longBlurb:
      "501(c) nonprofits file Form 990. The 990 draft surface is being built; talk to the CPA Tax Resolver in /chat for now."
  },
  trust: {
    formId: "1041",
    formName: "Form 1041",
    specialistPersona: "cpa-tax-resolver",
    specialistDisplayName: "CPA Tax Resolver",
    shortBlurb: "U.S. Income Tax Return for Estates and Trusts",
    longBlurb:
      "Trusts file Form 1041. The 1041 surface is being built; talk to the CPA Tax Resolver in /chat for now."
  },
  estate: {
    formId: "1041",
    formName: "Form 1041",
    specialistPersona: "cpa-tax-resolver",
    specialistDisplayName: "CPA Tax Resolver",
    shortBlurb: "U.S. Income Tax Return for Estates and Trusts",
    longBlurb:
      "Estates file Form 1041. The 1041 surface is being built; talk to the CPA Tax Resolver in /chat for now."
  }
};

const FALLBACK_VARIANT: FormVariant = FORM_BY_KIND.c_corp;

export function getFormVariant(kind: string | undefined): FormVariant {
  if (!kind) return FALLBACK_VARIANT;
  return FORM_BY_KIND[kind] ?? FALLBACK_VARIANT;
}

// ---------------------------------------------------------------------------
// Placeholder view (any kind that doesn't have a full surface yet)
// ---------------------------------------------------------------------------

export function TaxFormPlaceholder({
  workspaceId,
  workspaceSlug,
  entity,
  entityDisplayName,
  year,
  variant,
  isFallback,
  entities,
  canConsolidate
}: {
  workspaceId: string;
  workspaceSlug: string;
  entity: Entity | null;
  entityDisplayName: string;
  year: number;
  variant: FormVariant;
  isFallback: boolean;
  entities: Entity[];
  canConsolidate: boolean;
}) {
  const personaHref =
    `/chat?persona=${encodeURIComponent(variant.specialistPersona)}` +
    (entity ? `&entity_id=${encodeURIComponent(entity.id)}` : "") +
    `&tax_year=${year}`;

  return (
    <div className="space-y-4">
      <Card className="border-brand-500/40 bg-brand-500/5">
        <div className="flex items-start gap-3">
          <Sparkles size={20} className="mt-0.5 shrink-0 text-brand-300" />
          <div className="min-w-0 flex-1">
            <h2 className="text-base font-semibold text-ink-primary">
              {variant.formName} surface — work in progress for{" "}
              {entity?.kind ?? "this entity kind"}
            </h2>
            <p className="mt-1.5 text-sm leading-relaxed text-ink-secondary">
              {variant.longBlurb}
            </p>
            {isFallback ? (
              <p className="mt-2 text-xs leading-relaxed text-ink-tertiary">
                We default unknown entity kinds to Form 1120; if your entity
                kind is wrong, update it in workspace settings so the right
                form variant routes here automatically.
              </p>
            ) : null}
          </div>
        </div>
      </Card>

      <Card>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-secondary">
          What to do right now
        </h3>
        <ul className="space-y-2.5 text-sm text-ink-secondary">
          <li className="flex items-start gap-2">
            <span className="mt-0.5 text-brand-400">•</span>
            <span>
              Open a chat with the{" "}
              <Link
                href={personaHref}
                className="text-brand-400 underline-offset-2 hover:underline"
              >
                {variant.specialistDisplayName}
              </Link>{" "}
              — they can answer questions about {variant.formName} and draft
              memos / flagged positions that land in the CPA review queue.
            </span>
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 text-brand-400">•</span>
            <span>
              Use{" "}
              <Link
                href="/open-items"
                className="text-brand-400 underline-offset-2 hover:underline"
              >
                /open-items
              </Link>{" "}
              to track time-sensitive deadlines, documents you owe, and
              decisions queued for you.
            </span>
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 text-brand-400">•</span>
            <span>
              For prior or other-kind entities in this workspace use the
              entity selector below to switch context.
            </span>
          </li>
        </ul>

        <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-subtle pt-3 text-[11px] text-ink-tertiary">
          <span>
            Workspace: {workspaceSlug} · Entity: {entityDisplayName} · Year:{" "}
            {year}
          </span>
          <Link
            href={personaHref}
            className="inline-flex items-center gap-1.5 rounded-md border border-default bg-surface px-3 py-1.5 text-xs font-medium text-ink-primary transition hover:bg-elevated"
          >
            <MessageSquare size={12} />
            Open chat with {variant.specialistDisplayName}
            <ExternalLink size={11} />
          </Link>
        </div>
      </Card>

      {/* Entity selector — duplicated near the placeholder body so the user
          can hop into a C-Corp entity (if any) without scrolling back to
          the page header. */}
      <Card>
        <p className="mb-2 text-xs text-ink-secondary">
          Switch entity (other entities in this workspace may have a full
          surface):
        </p>
        <EntitySelector
          entities={entities}
          activeEntityId={entity?.id}
          canConsolidate={canConsolidate}
        />
      </Card>

      <p className="text-[11px] text-ink-tertiary">
        Workspace id: <code className="text-ink-secondary">{workspaceId}</code>
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dispatcher
// ---------------------------------------------------------------------------

export type EntityKindRouterProps = {
  workspace: Workspace;
  entity: Entity | null;
  entities: Entity[];
  isConsolidated: boolean;
  canConsolidate: boolean;
  year: number;
};

export async function EntityKindRouter({
  workspace,
  entity,
  entities,
  isConsolidated,
  canConsolidate,
  year
}: EntityKindRouterProps) {
  // Consolidated view always uses the 1120 surface (it aggregates all
  // entities; the C-Corp narrative is the most informative shape). The
  // existing banner-for-non-C-Corp logic in year-view doesn't trigger here.
  const kind = isConsolidated ? "c_corp" : entity?.kind ?? "c_corp";
  const variant = getFormVariant(kind);
  const isFallback = kind !== "c_corp" && variant === FALLBACK_VARIANT;

  const entityDisplayName = isConsolidated
    ? "All entities (consolidated)"
    : entity?.display_name ?? "—";

  // Only render the full Form-1120 YearView for C-Corp (the canonical 1120
  // surface) or consolidated views. Everything else gets the placeholder.
  if (variant.formId === "1120") {
    const entityId =
      isConsolidated || !entity ? "all" : entity.id;
    let pkg = null;
    let fetchError: string | null = null;
    try {
      pkg = await fetch1120Package(workspace.id, entityId, year);
    } catch (e) {
      fetchError =
        e instanceof Error ? e.message : "Failed to load Form 1120 package.";
    }
    const [reviewQueue, specialistRunsResp] = await Promise.all([
      fetchTaxReviewQueue(workspace.id, entityId, year),
      fetchSpecialistRuns(workspace.id, { entityId, taxYear: year })
    ]);

    return (
      <>
        {/* Wrong-entity-kind banner kept for consistency — same logic the
            old page.tsx used. Renders only when we landed on the 1120
            surface as a fallback (kind != c_corp, not consolidated). */}
        {!isConsolidated && entity && entity.kind !== "c_corp" ? (
          <Card className="border-brand-500/40 bg-brand-500/5 mb-3">
            <p className="text-sm text-ink-primary">
              Routed to Form 1120 as a default — entity{" "}
              <strong>{entityDisplayName}</strong> is kind &ldquo;{entity.kind}
              &rdquo; without a dedicated surface yet.
            </p>
          </Card>
        ) : null}
        {fetchError ? (
          <Card>
            <p className="text-sm text-debit">{fetchError}</p>
            <p className="mt-1 text-xs text-ink-tertiary">
              Make sure the entity has posted journal entries for {year} and the
              cpa-export service is running.
            </p>
          </Card>
        ) : (
          <YearView
            pkg={pkg}
            workspaceId={workspace.id}
            workspaceSlug={workspace.slug}
            entityId={entityId}
            entityDisplayName={entityDisplayName}
            year={year}
            taxReviewItems={reviewQueue.items}
            specialistRuns={specialistRunsResp.items}
            canRunDrafts={
              canRunDrafts(workspace.role) &&
              !isConsolidated &&
              entity?.kind === "c_corp"
            }
          />
        )}
      </>
    );
  }

  // Non-C-Corp -> placeholder routes to the right specialist.
  return (
    <TaxFormPlaceholder
      workspaceId={workspace.id}
      workspaceSlug={workspace.slug}
      entity={entity}
      entityDisplayName={entityDisplayName}
      year={year}
      variant={variant}
      isFallback={isFallback}
      entities={entities}
      canConsolidate={canConsolidate}
    />
  );
}

// ---------------------------------------------------------------------------
// Helpers exported for the page header (so the title reflects the form
// instead of always saying "Form 1120").
// ---------------------------------------------------------------------------

export function titleFor(
  isConsolidated: boolean,
  entity: Entity | null,
  year: number
): { title: string; subtitle: string } {
  if (isConsolidated) {
    return {
      title: `Form 1120 — ${year}`,
      subtitle: `All entities (consolidated) · ${FORM_BY_KIND.c_corp.shortBlurb}`
    };
  }
  const variant = getFormVariant(entity?.kind);
  return {
    title: `${variant.formName} — ${year}`,
    subtitle: `${entity?.display_name ?? "—"} · ${variant.shortBlurb}`
  };
}

export const _FORM_BY_KIND = FORM_BY_KIND;

import Link from "next/link";
import {
  ArrowRight,
  FileText,
  ShieldCheck,
  Banknote,
  Sparkles,
  Layers,
  SkipForward
} from "lucide-react";
import { AppShell } from "@/components/app-shell";
import {
  Card,
  EmptyState,
  MaterialityBadge,
  PageShell,
  SectionHeader,
  StatusChip
} from "@/components/primitives";
import { ApprovalActions } from "@/components/approval-actions";
import { JournalProposalDetails, canReviewJournalProposal } from "@/components/journal-proposal-details";
import { BankTxnBatchPanel } from "@/components/bank-txn-batch-panel";
import { RecommendationsView } from "@/app/approvals/recommendations-view";
import { ClusterReview } from "@/components/approvals/cluster-review";
import { SkippedQueue } from "@/components/approvals/skipped-queue";
import { getActiveContext } from "@/lib/active-context";
import {
  getApprovals,
  getApprovalCategories,
  type Approval
} from "@/lib/approvals";
import { getApprovalRecommendations } from "@/lib/approval-recommendations";
import { getApprovalClusters } from "@/lib/approval-clusters";
import { formatAmount } from "@/lib/journal";

// -----------------------------------------------------------------------
// Tab definitions
// -----------------------------------------------------------------------

type Tab = "review" | "rows" | "recommendations" | "skipped";

// -----------------------------------------------------------------------
// Helpers for journal_recategorization + compliance_rule rows
// (bank_transaction_import rows are handled by BankTxnBatchPanel)
// -----------------------------------------------------------------------

function subjectLabel(a: Approval): string {
  if (a.subject_type === "journal_recategorization") return "Recategorization";
  if (a.subject_type === "compliance_rule") return "Compliance rule";
  if (a.subject_type === "journal_entry_post") return "Journal entry";
  return a.subject_type;
}

function ApprovalCard({
  approval,
  workspaceId,
  canDecide
}: {
  approval: Approval;
  workspaceId: string;
  canDecide: boolean;
}) {
  const p = approval.payload ?? {};
  const tier = (p.materiality_tier ?? "") as "L" | "M" | "H" | "";
  const isRecat = approval.subject_type === "journal_recategorization";
  const isJournal = approval.subject_type === "journal_entry_post";
  const journalScopeMatches = p.workspace_id === workspaceId && p.entity_id === approval.entity_id;
  const journalReviewable = !isJournal || (
    canReviewJournalProposal(p) && journalScopeMatches
  );

  return (
    <Card>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1.5 text-sm font-semibold">
              {isRecat ? <FileText size={14} /> : <ShieldCheck size={14} />}
              {subjectLabel(approval)}
            </span>
            {tier && (tier === "L" || tier === "M" || tier === "H") && (
              <MaterialityBadge level={tier} />
            )}
            {approval.entity_display_name && (
              <StatusChip tone="ink">{approval.entity_display_name}</StatusChip>
            )}
            {approval.amount && (
              <span className="font-mono text-xs text-ink-secondary">
                {formatAmount(approval.amount, approval.currency_code ?? "USD")}
              </span>
            )}
          </div>

          {isRecat && p.from_category && p.new_category && (
            <div className="mt-2 flex items-center gap-2 text-sm">
              <span className="rounded bg-white/5 px-2 py-0.5 text-ink-secondary">
                {p.from_category}
              </span>
              <ArrowRight size={13} className="text-ink-tertiary" />
              <span className="rounded bg-brand-500/10 px-2 py-0.5 text-brand-300">
                {p.new_category}
              </span>
            </div>
          )}

          {!isRecat && p.kind && (
            <p className="mt-2 font-mono text-[11px] text-ink-tertiary">
              {p.kind}
              {p.trigger ? ` · trigger=${p.trigger}` : ""}
              {p.action_enum ? ` · action=${p.action_enum}` : ""}
            </p>
          )}

          <p className="mt-3 text-sm leading-relaxed text-ink-secondary">
            {approval.reasoning}
          </p>

          {isJournal && journalScopeMatches && <JournalProposalDetails payload={p} />}
          {isJournal && !journalScopeMatches &&
            <p role="alert" className="mt-3 text-sm text-ink-secondary">This request's workspace or entity does not match the approval. Review is unavailable.</p>}

          <p className="mt-3 text-[11px] text-ink-tertiary">
            Requested by{" "}
            <span className="font-mono">{approval.requested_by_identity}</span>{" "}
            · {new Date(approval.created_at).toLocaleString()}
          </p>
        </div>

        <div className="shrink-0">
          {journalReviewable && <ApprovalActions
            workspaceId={workspaceId}
            approvalId={approval.id}
            canDecide={canDecide}
          />}
        </div>
      </div>
    </Card>
  );
}

// -----------------------------------------------------------------------
// Tab bar (server-rendered pill nav using search params)
// -----------------------------------------------------------------------

function TabBar({
  active,
  reviewCount,
  btiCount,
  recCount,
  skippedCount
}: {
  active: Tab;
  reviewCount: number;
  btiCount: number;
  recCount: number;
  skippedCount: number;
}) {
  const tabs: { id: Tab; label: string; count?: number; icon: React.ReactNode }[] = [
    {
      id: "review",
      label: "Review",
      count: reviewCount,
      icon: <Layers size={13} />
    },
    {
      id: "rows",
      label: "All rows",
      count: btiCount,
      icon: <Banknote size={13} />
    },
    {
      id: "recommendations",
      label: "AI scoring",
      count: recCount,
      icon: <Sparkles size={13} />
    },
    {
      id: "skipped",
      label: "Skipped",
      count: skippedCount,
      icon: <SkipForward size={13} />
    }
  ];

  return (
    <div className="flex items-center gap-1 overflow-x-auto rounded-lg border border-default bg-elevated p-1">
      {tabs.map((t) => (
        <Link
          key={t.id}
          href={`/approvals${t.id !== "review" ? `?tab=${t.id}` : ""}`}
          className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
            active === t.id
              ? "bg-surface text-ink-primary shadow-sm"
              : "text-ink-tertiary hover:text-ink-secondary"
          }`}
        >
          {t.icon}
          {t.label}
          {t.count != null && t.count > 0 && (
            <span
              className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                active === t.id
                  ? t.id === "recommendations"
                    ? "bg-credit/20 text-credit"
                    : "bg-brand-500/20 text-brand-300"
                  : "bg-white/5 text-ink-tertiary"
              }`}
            >
              {t.count}
            </span>
          )}
        </Link>
      ))}
    </div>
  );
}

// -----------------------------------------------------------------------
// Page
// -----------------------------------------------------------------------

export default async function ApprovalsPage({
  searchParams
}: {
  searchParams: Promise<{ tab?: string }>;
}) {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const entityId = ctx.isConsolidated ? undefined : ctx.activeEntity?.id;
  const sp = await searchParams;
  const activeTab: Tab =
    sp.tab === "rows"
      ? "rows"
      : sp.tab === "recommendations"
        ? "recommendations"
        : sp.tab === "skipped"
          ? "skipped"
          : "review"; // default to the new cluster-card UX

  const [data, skippedData, categories, recsData, clusters] = await Promise.all([
    workspace
      ? getApprovals(workspace.id, { entityId, status: "pending" })
      : null,
    workspace
      ? getApprovals(workspace.id, { entityId, status: "skipped" })
      : null,
    workspace ? getApprovalCategories(workspace.id) : [],
    workspace
      ? getApprovalRecommendations(workspace.id, {
          entityId,
          status: "pending",
          limit: 500
        })
      : null,
    workspace ? getApprovalClusters(workspace.id, { entityId }) : null
  ]);

  const approvals = data?.approvals ?? [];
  const skippedApprovals = skippedData?.approvals ?? [];
  const skippedCount = skippedData?.total ?? skippedApprovals.length;
  const canDecide = workspace
    ? ["owner", "admin"].includes(workspace.role)
    : false;

  // Split by type — BTI rows go to the interactive batch panel
  const btiApprovals = approvals.filter(
    (a) => a.subject_type === "bank_transaction_import"
  );
  const otherApprovals = approvals.filter(
    (a) => a.subject_type !== "bank_transaction_import"
  );

  const totalPending = approvals.length;
  const recCount = recsData?.recommendations.length ?? 0;
  const recClusters = recsData?.clusters ?? [];
  const recRows = recsData?.recommendations ?? [];

  // Review-tab count = number of (cluster + singleton) decisions remaining
  const reviewCount = clusters
    ? clusters.clusters.length + clusters.singletons.length
    : 0;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Pending approvals"
          subtitle={
            workspace
              ? `${totalPending} material ${
                  totalPending === 1 ? "action" : "actions"
                } awaiting human attestation`
              : "Material actions an agent queued, waiting for a human to attest."
          }
          action={
            totalPending > 0 ? (
              <StatusChip tone="pending">{totalPending} pending</StatusChip>
            ) : undefined
          }
        />

        {!workspace ? (
          <Card>
            <p className="text-sm text-ink-tertiary">
              No active workspace. Pick one from the switcher above.
            </p>
          </Card>
        ) : (
          <div className="flex flex-col gap-6">
            {/* Tab bar (show only when there is something to switch to). */}
            {(reviewCount > 0 || btiApprovals.length > 0 || skippedCount > 0) && (
              <TabBar
                active={activeTab}
                reviewCount={reviewCount}
                btiCount={btiApprovals.length}
                recCount={recCount}
                skippedCount={skippedCount}
              />
            )}

            {!canDecide && (
              <div className="rounded-lg border border-pending/40 bg-pending/[0.06] px-5 py-3 text-xs text-ink-secondary">
                You can review these, but only an <strong>admin</strong> can
                attest (approve/apply) a material action.
              </div>
            )}

            {totalPending === 0 && reviewCount === 0 ? (
              // H9: ported to shared EmptyState primitive (celebratory "caught
              // up" variant — preserves the existing tone but standardises the
              // layout to match transactions/journal/reconciliation).
              <EmptyState
                variant="no-data"
                icon={
                  <ShieldCheck
                    size={28}
                    className="text-credit"
                    aria-hidden="true"
                  />
                }
                title="Books are caught up. Nothing waiting on you."
                description={
                  <>
                    Material recategorizations, proposed compliance rules, and
                    bank-import reviews land here. Lower-value items live in
                    the{" "}
                    <Link
                      href="/reconciliation-queue"
                      className="text-brand-400 underline-offset-2 hover:underline"
                    >
                      Reconciliation queue
                    </Link>
                    .
                  </>
                }
              />
            ) : activeTab === "review" && clusters ? (
              /* ---- Cluster cards + singleton swipe (default tab) ---- */
              <>
                <ClusterReview
                  data={clusters}
                  categories={categories}
                  workspaceId={workspace.id}
                  canDecide={canDecide}
                />
                {otherApprovals.length > 0 && (
                  <section className="mt-2">
                    <div className="mb-3 flex items-center gap-2">
                      <h2 className="text-sm font-semibold">
                        Other pending approvals
                      </h2>
                      <StatusChip tone="pending">
                        {otherApprovals.length} pending
                      </StatusChip>
                    </div>
                    <div className="flex flex-col gap-3">
                      {otherApprovals.map((a) => (
                        <ApprovalCard
                          key={a.id}
                          approval={a}
                          workspaceId={workspace.id}
                          canDecide={canDecide}
                        />
                      ))}
                    </div>
                  </section>
                )}
              </>
            ) : activeTab === "recommendations" ? (
              /* ---- AI Recommendations tab (legacy view) ---- */
              <RecommendationsView
                recommendations={recRows}
                clusters={recClusters}
                workspaceId={workspace.id}
                canDecide={canDecide}
              />
            ) : activeTab === "skipped" ? (
              /* ---- Skipped queue (decide later) ---- */
              <SkippedQueue
                approvals={skippedApprovals}
                workspaceId={workspace.id}
                canDecide={canDecide}
              />
            ) : (
              /* ---- Rows / Grouped tab (legacy flat list) ---- */
              <div className="flex flex-col gap-8">
                {/* ---- bank_transaction_import section ---- */}
                {btiApprovals.length > 0 && (
                  <section>
                    <div className="mb-3 flex items-center gap-2">
                      <Banknote size={16} className="text-ink-secondary" />
                      <h2 className="text-sm font-semibold">
                        Bank import review
                      </h2>
                      <StatusChip tone="pending">
                        {btiApprovals.length} pending
                      </StatusChip>
                    </div>
                    <BankTxnBatchPanel
                      approvals={btiApprovals}
                      workspaceId={workspace.id}
                      canDecide={canDecide}
                      categories={categories}
                    />
                  </section>
                )}

                {/* ---- journal_recategorization + compliance_rule section ---- */}
                {otherApprovals.length > 0 && (
                  <section>
                    {btiApprovals.length > 0 && (
                      <div className="mb-3 flex items-center gap-2">
                        <h2 className="text-sm font-semibold">
                          Other pending approvals
                        </h2>
                        <StatusChip tone="pending">
                          {otherApprovals.length} pending
                        </StatusChip>
                      </div>
                    )}
                    <div className="flex flex-col gap-3">
                      {otherApprovals.map((a) => (
                        <ApprovalCard
                          key={a.id}
                          approval={a}
                          workspaceId={workspace.id}
                          canDecide={canDecide}
                        />
                      ))}
                    </div>
                  </section>
                )}
              </div>
            )}
          </div>
        )}
      </PageShell>
    </AppShell>
  );
}

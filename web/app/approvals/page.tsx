import Link from "next/link";
import { ArrowRight, FileText, ShieldCheck, Banknote } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import {
  Card,
  MaterialityBadge,
  PageShell,
  SectionHeader,
  StatusChip
} from "@/components/primitives";
import { ApprovalActions } from "@/components/approval-actions";
import { BankTxnBatchPanel } from "@/components/bank-txn-batch-panel";
import { getActiveContext } from "@/lib/active-context";
import {
  getApprovals,
  getApprovalCategories,
  type Approval
} from "@/lib/approvals";
import { formatAmount } from "@/lib/journal";

// -----------------------------------------------------------------------
// Helpers for journal_recategorization + compliance_rule rows
// (bank_transaction_import rows are handled by BankTxnBatchPanel)
// -----------------------------------------------------------------------

function subjectLabel(a: Approval): string {
  if (a.subject_type === "journal_recategorization") return "Recategorization";
  if (a.subject_type === "compliance_rule") return "Compliance rule";
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

          <p className="mt-3 text-[11px] text-ink-tertiary">
            Requested by{" "}
            <span className="font-mono">{approval.requested_by_identity}</span>{" "}
            · {new Date(approval.created_at).toLocaleString()}
          </p>
        </div>

        <div className="shrink-0">
          <ApprovalActions
            workspaceId={workspaceId}
            approvalId={approval.id}
            canDecide={canDecide}
          />
        </div>
      </div>
    </Card>
  );
}

// -----------------------------------------------------------------------
// Page
// -----------------------------------------------------------------------

export default async function ApprovalsPage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const entityId = ctx.isConsolidated ? undefined : ctx.activeEntity?.id;

  const [data, categories] = await Promise.all([
    workspace
      ? getApprovals(workspace.id, { entityId, status: "pending" })
      : null,
    workspace ? getApprovalCategories(workspace.id) : []
  ]);

  const approvals = data?.approvals ?? [];
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
        ) : totalPending === 0 ? (
          <Card>
            <p className="text-sm text-ink-tertiary">
              Nothing waiting on you — books are caught up. Material
              recategorizations, proposed compliance rules, and bank import
              reviews show up here. Lower-value items are in the{" "}
              <Link
                href="/reconciliation-queue"
                className="text-brand-400 underline-offset-2 hover:underline"
              >
                Reconciliation queue
              </Link>
              .
            </p>
          </Card>
        ) : (
          <div className="flex flex-col gap-8">
            {!canDecide && (
              <div className="rounded-xl border border-pending/40 bg-pending/[0.06] px-5 py-3 text-xs text-ink-secondary">
                You can review these, but only an <strong>admin</strong> can
                attest (approve/apply) a material action.
              </div>
            )}

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
      </PageShell>
    </AppShell>
  );
}

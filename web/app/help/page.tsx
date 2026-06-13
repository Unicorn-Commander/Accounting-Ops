import Link from "next/link";
import {
  BarChart3,
  Book,
  Bot,
  CheckCircle2,
  Edit3,
  Filter,
  HelpCircle,
  Lock,
  Shield,
  Workflow,
  XCircle
} from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { EmbeddedChat } from "@/components/chat/embedded-chat";
import { getActiveContext } from "@/lib/active-context";
import { fetchChatPersonas } from "@/lib/chat";

export const dynamic = "force-dynamic";

const GUIDE_SLUG = "product-guide";

const GUIDE_PROMPTS = [
  "How do I reconcile a transaction?",
  "How do I record an owner contribution?",
  "What does the materiality gate do?",
  "How do I hand off to my CPA at year-end?"
];

export default async function HelpPage() {
  // Best-effort: resolve the active workspace + entity + personas so we can
  // mount the live Guide agent at the top. If anything is missing (no
  // workspace yet, or the persona isn't registered on the backend), we fall
  // back to the static help below — the page must never break.
  const ctx = await getActiveContext().catch(() => null);
  const entity = ctx?.activeWorkspace
    ? ctx.activeEntity ?? ctx.entities[0] ?? null
    : null;
  const personasRes =
    ctx?.activeWorkspace && entity ? await fetchChatPersonas() : null;
  const personas = personasRes?.personas ?? [];
  const guideAvailable =
    !!ctx?.activeWorkspace &&
    !!entity &&
    personas.some((p) => p.slug === GUIDE_SLUG);

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Help"
          subtitle="Ask the Guide, or browse what each page does, the common workflows, and where to dig in deeper."
        />

        {guideAvailable && ctx?.activeWorkspace && entity && (
          <section aria-label="Ask the Guide" className="mb-10">
            <div className="overflow-hidden rounded-xl border border-default bg-elevated/50 shadow-card">
              <div className="flex items-center gap-2.5 border-b border-subtle px-4 py-3">
                <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-agent-500/15 text-agent-500">
                  <Bot size={17} />
                </span>
                <div className="min-w-0">
                  <h2 className="text-sm font-semibold text-ink-primary">Ask the Guide</h2>
                  <p className="text-xs text-ink-tertiary">
                    A real agent that knows how Accounting-Ops works — ask it
                    anything, like &ldquo;how do I&hellip;?&rdquo;
                  </p>
                </div>
              </div>
              <div className="h-[460px]">
                <EmbeddedChat
                  workspaceId={ctx.activeWorkspace.id}
                  entityId={entity.id}
                  entityName={entity.display_name}
                  personas={personas}
                  defaultPersonaSlug={GUIDE_SLUG}
                  showPersonaPicker={false}
                  emptyTitle="How can I help you use Accounting-Ops?"
                  emptyHint="I can show you where things are, walk you through a workflow, or explain what a screen means. Try one of these:"
                  suggestedPrompts={GUIDE_PROMPTS}
                  className="h-full"
                />
              </div>
            </div>
          </section>
        )}

        {/* Tour grid */}
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-ink-tertiary">
          Pages
        </h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          <PageCard
            icon={<BarChart3 size={16} className="text-brand-300" />}
            title="Dashboard"
            href="/dashboard"
            blurb="Top-of-funnel KPIs. Cash position from your banks; YTD revenue, expenses, and net income from the journal. Click Recent Transactions to see live activity."
          />
          <PageCard
            icon={<Filter size={16} className="text-brand-300" />}
            title="Transactions"
            href="/transactions"
            blurb="Every live money event across SimpleFIN (BofA + USAA) and eBay orders. Filter by bank, account, category, or free text. Entity badges show ownership."
          />
          <PageCard
            icon={<Workflow size={16} className="text-pending" />}
            title="Reconciliation Queue"
            href="/reconciliation-queue"
            blurb="Where you spend most time. Three columns: needs review, uncategorized, auto-categorized. Click Confirm to post, Reject to train a new rule."
          />
          <PageCard
            icon={<Book size={16} className="text-credit" />}
            title="Journal"
            href="/journal"
            blurb="The append-only book of record. Hash-chained per entity. Click any row to see full entry detail + provenance + reversal lineage. Filter + verify chain inline."
          />
          <PageCard
            icon={<Edit3 size={16} className="text-credit" />}
            title="Manual entry"
            href="/journal/new"
            blurb="For things that don't come from connectors: owner contributions, cash sales, year-end adjustments, reclasses. Same hash chain as auto-posted entries."
          />
          <PageCard
            icon={<BarChart3 size={16} className="text-credit" />}
            title="Reports"
            href="/reports"
            blurb="Live P&L, Balance Sheet, Cash Flow. Period chips for MTD/QTD/YTD/quarters. Balance-check on BS verifies A = L + E. Drill down via Export."
          />
          <PageCard
            icon={<Book size={16} className="text-credit" />}
            title="CPA Export"
            href="/cpa-export"
            blurb="Downloadable CSV / IIF / QBO / PDF for handing to your CPA. Period + entity scoped. Hash column in CSV for chain verification."
          />
          <PageCard
            icon={<Lock size={16} className="text-brand-300" />}
            title="Connectors"
            href="/connectors"
            blurb="Bank + marketplace connections. SimpleFIN claims. Account-to-entity mapping powers entity filtering everywhere."
          />
        </div>

        {/* Common workflows */}
        <h2 className="mb-3 mt-10 text-sm font-semibold uppercase tracking-wider text-ink-tertiary">
          Common workflows
        </h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Card>
            <h3 className="mb-2 text-sm font-semibold">Daily 5-minute review</h3>
            <ol className="ml-4 list-decimal space-y-1.5 text-sm text-ink-secondary">
              <li>Open <Link href="/dashboard" className="text-brand-400 hover:underline">/dashboard</Link> — eyeball YTD numbers + recent activity</li>
              <li>Open <Link href="/reconciliation-queue" className="text-brand-400 hover:underline">/reconciliation-queue</Link></li>
              <li>Skim Auto-categorized → click Post on anything you recognize</li>
              <li>Needs Review → read each, Confirm or Reject</li>
              <li>Uncategorized → Reject + train new rules for recurring vendors</li>
            </ol>
          </Card>

          <Card>
            <h3 className="mb-2 text-sm font-semibold">End of month</h3>
            <ol className="ml-4 list-decimal space-y-1.5 text-sm text-ink-secondary">
              <li><Link href="/reports?view=pl" className="text-brand-400 hover:underline">/reports</Link> → P&L for the month, sanity check</li>
              <li>Balance Sheet tab → confirm A = L + E (drift should be $0.00)</li>
              <li><Link href="/journal" className="text-brand-400 hover:underline">/journal</Link> → filter to that month, spot-check entries</li>
              <li>Click "verify chain" on each entity card → confirm green</li>
              <li><Link href="/cpa-export" className="text-brand-400 hover:underline">/cpa-export</Link> → CSV + PDF P&L → archive</li>
            </ol>
          </Card>

          <Card>
            <h3 className="mb-2 text-sm font-semibold">Year-end CPA handoff</h3>
            <ol className="ml-4 list-decimal space-y-1.5 text-sm text-ink-secondary">
              <li>Post all manual adjustments (depreciation, accruals) via <Link href="/journal/new" className="text-brand-400 hover:underline">/journal/new</Link></li>
              <li><Link href="/reports" className="text-brand-400 hover:underline">/reports</Link> → full-year P&L + as-of Dec 31 Balance Sheet</li>
              <li><Link href="/cpa-export" className="text-brand-400 hover:underline">/cpa-export</Link> → IIF + CSV + PDFs per entity</li>
              <li>Verify chain on every entity — screenshot the green badges</li>
              <li>Email the bundle to your CPA. They import IIF directly into QuickBooks Desktop.</li>
            </ol>
          </Card>

          <Card>
            <h3 className="mb-2 text-sm font-semibold">Rejecting + training the system</h3>
            <ol className="ml-4 list-decimal space-y-1.5 text-sm text-ink-secondary">
              <li>On <Link href="/reconciliation-queue" className="text-brand-400 hover:underline">/reconciliation-queue</Link>, click the X next to a wrongly-categorized row</li>
              <li>Edit the pattern (defaults to most distinctive payee word)</li>
              <li>Pick the correct category from the dropdown</li>
              <li>Save → future txns matching that pattern auto-categorize</li>
              <li>The override is stored per-workspace; optionally per-entity</li>
            </ol>
          </Card>
        </div>

        {/* Concepts */}
        <h2 className="mb-3 mt-10 text-sm font-semibold uppercase tracking-wider text-ink-tertiary">
          Concepts
        </h2>
        <div className="space-y-3">
          <Card>
            <div className="flex items-start gap-3">
              <Shield className="mt-0.5 shrink-0 text-credit" size={18} />
              <div>
                <h3 className="text-sm font-semibold">Append-only journal with hash chain</h3>
                <p className="mt-1 text-sm text-ink-secondary">
                  Every posted entry is part of a per-entity SHA-256 hash chain. Reversals create
                  <em> new</em> entries — we never overwrite. The DB enforces uniqueness on
                  (entity_id, this_hash) so concurrent posts can't fork the chain. Anyone can
                  click "verify chain" on /journal to walk the entire history and confirm
                  integrity.
                </p>
              </div>
            </div>
          </Card>

          <Card>
            <div className="flex items-start gap-3">
              <CheckCircle2 className="mt-0.5 shrink-0 text-credit" size={18} />
              <div>
                <h3 className="text-sm font-semibold">Materiality gate</h3>
                <p className="mt-1 text-sm text-ink-secondary">
                  Three tiers at posting time:
                </p>
                <ul className="mt-2 ml-4 list-disc space-y-1 text-sm text-ink-secondary">
                  <li><StatusChip tone="credit">L</StatusChip> &lt; $75 — auto-post when category is trusted + entity mapped + sign matches</li>
                  <li><StatusChip tone="pending">M</StatusChip> $75 – $1,000 — queue for human Confirm</li>
                  <li><StatusChip tone="ink">H</StatusChip> &gt; $1,000 — hard block, requires explicit Confirm</li>
                </ul>
                <p className="mt-2 text-sm text-ink-secondary">
                  IRS §274(d) override: travel/meals always block without a matching receipt,
                  regardless of amount.
                </p>
              </div>
            </div>
          </Card>

          <Card>
            <div className="flex items-start gap-3">
              <Lock className="mt-0.5 shrink-0 text-brand-300" size={18} />
              <div>
                <h3 className="text-sm font-semibold">Encrypted credentials</h3>
                <p className="mt-1 text-sm text-ink-secondary">
                  Connector secrets (SimpleFIN access URLs, eBay refresh tokens, IMAP creds) are
                  encrypted at rest with AES-256-GCM. The per-workspace data-encryption key is
                  derived via HKDF-SHA256 from a master key. The master key lives in Docker
                  secrets at <code className="font-mono">/run/secrets/accounting_ops_master_key</code>,
                  never written to disk in plaintext, never logged.
                </p>
              </div>
            </div>
          </Card>

          <Card>
            <div className="flex items-start gap-3">
              <XCircle className="mt-0.5 shrink-0 text-pending" size={18} />
              <div>
                <h3 className="text-sm font-semibold">Sign mismatches</h3>
                <p className="mt-1 text-sm text-ink-secondary">
                  When a vendor rule expects revenue (e.g. "stripe") but the transaction is
                  negative (or vice versa), the categorization gets flagged
                  <code className="font-mono"> sign_matched=false</code>. The materiality gate
                  refuses to auto-post these even at tier L — they always go to the queue.
                  Usually means either a refund or that the rule needs an override.
                </p>
              </div>
            </div>
          </Card>
        </div>

        {/* Deeper */}
        <h2 className="mb-3 mt-10 text-sm font-semibold uppercase tracking-wider text-ink-tertiary">
          Dig deeper
        </h2>
        <Card>
          <ul className="space-y-2 text-sm">
            <DocLink path="docs/USER-GUIDE.md" label="Full user guide" />
            <DocLink path="docs/ARCHITECTURE.md" label="Runtime architecture + data flow + hash chain spec" />
            <DocLink path="docs/OPERATIONS.md" label="Deploy, monitor, backup, restore" />
            <DocLink path="docs/PHASE-1-6-ENCRYPTED-CREDENTIALS.md" label="Encrypted credentials threat model" />
            <DocLink path="docs/PHASE-2-RECEIPT-WORKER-BOOTSTRAP.md" label="Receipt worker setup" />
            <DocLink path="docs/PHASE-2-RECEIPT-VLM-PIPELINE.md" label="Receipt VLM pipeline (Tier 1/2/3)" />
            <DocLink path="docs/STALWART-IMAP-BOOTSTRAP.md" label="Stalwart IMAP mailbox setup" />
            <DocLink path="docs/WORKSPACE-ENTITY-USER-MODEL.md" label="Multi-tenancy model" />
            <DocLink path="docs/AGENT-ACTIVITY-SPEC.md" label="Agent activity surface (planned)" />
          </ul>
        </Card>

        <p className="mt-8 text-[11px] text-ink-tertiary">
          Need help that&apos;s not here? Ask the Guide above, or open the{" "}
          <Link href="/chat" className="text-brand-400 hover:underline">
            full chat
          </Link>
          {" "}to reach the CFO and tax agents.
        </p>
      </PageShell>
    </AppShell>
  );
}

function PageCard({
  icon,
  title,
  href,
  blurb
}: {
  icon: React.ReactNode;
  title: string;
  href: string;
  blurb: string;
}) {
  return (
    <Card className="hover:border-strong">
      <Link href={href} className="block">
        <div className="mb-2 flex items-center gap-2">
          {icon}
          <h3 className="text-sm font-semibold">{title}</h3>
        </div>
        <p className="text-xs leading-relaxed text-ink-secondary">{blurb}</p>
        <p className="mt-2 text-[10px] font-mono text-ink-tertiary">{href}</p>
      </Link>
    </Card>
  );
}

function DocLink({ path, label }: { path: string; label: string }) {
  return (
    <li className="flex items-center gap-2">
      <HelpCircle size={11} className="shrink-0 text-ink-tertiary" />
      <span>
        <span className="text-ink-primary">{label}</span>{" "}
        <span className="font-mono text-[10px] text-ink-tertiary">{path}</span>
      </span>
    </li>
  );
}

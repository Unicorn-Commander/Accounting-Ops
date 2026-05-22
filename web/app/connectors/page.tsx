import {
  Banknote,
  CheckCircle2,
  ExternalLink,
  Mail,
  ShoppingBag,
  Wallet,
  XCircle,
  Clock,
  Plus
} from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { CONNECTORS, getConnectorStatuses, type Connector, type ConnectorStatus } from "@/lib/connectors";
import { cn } from "@/lib/cn";

const CATEGORY_ICON: Record<Connector["category"], React.ComponentType<{ size?: number; className?: string }>> = {
  banking: Banknote,
  marketplace: ShoppingBag,
  accounting: Wallet,
  email: Mail,
  payroll: Wallet
};

export default async function ConnectorsPage() {
  const statuses = await getConnectorStatuses();
  const available = CONNECTORS.filter((c) => c.available);
  const upcoming = CONNECTORS.filter((c) => !c.available);

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Connectors"
          subtitle="Read-only data sources the agents pull from. Each connector is a standalone MCP server in your stack. Add credentials per-entity in the workspace's connector state (Phase 1.5)."
        />

        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-ink-tertiary">
          Available now
        </h2>
        <div className="mb-12 grid grid-cols-1 gap-4 md:grid-cols-2">
          {available.map((connector) => (
            <ConnectorCard
              key={connector.id}
              connector={connector}
              status={statuses[connector.id]}
            />
          ))}
        </div>

        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-ink-tertiary">
          On the roadmap
        </h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {upcoming.map((connector) => (
            <UpcomingConnectorCard key={connector.id} connector={connector} />
          ))}
        </div>
      </PageShell>
    </AppShell>
  );
}

function ConnectorCard({
  connector,
  status
}: {
  connector: Connector;
  status?: ConnectorStatus;
}) {
  const Icon = CATEGORY_ICON[connector.category];
  return (
    <Card>
      <div className="flex items-start gap-4">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-elevated text-brand-400 ring-1 ring-inset ring-brand-500/30">
          <Icon size={18} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h3 className="text-base font-semibold">{connector.name}</h3>
              <p className="mt-0.5 text-[10px] uppercase tracking-wider text-ink-tertiary">
                {connector.category} · {connector.monthly_cost}
              </p>
            </div>
            <StatusBadge status={status} />
          </div>
          <p className="mt-3 text-sm leading-relaxed text-ink-secondary">
            {connector.description}
          </p>

          <ul className="mt-4 space-y-1 text-[12px] text-ink-secondary">
            {connector.what_it_enables.map((item) => (
              <li key={item} className="flex items-start gap-1.5">
                <CheckCircle2 size={11} className="mt-1 shrink-0 text-credit/80" />
                <span>{item}</span>
              </li>
            ))}
          </ul>

          {status?.state === "online" && (
            <details className="mt-3 text-[11px]">
              <summary className="cursor-pointer text-ink-tertiary hover:text-ink-primary">
                Diagnostic JSON
              </summary>
              <pre className="mt-2 overflow-auto rounded bg-canvas/60 p-2 font-mono text-[10px] text-ink-secondary">
                {JSON.stringify(status.details, null, 2)}
              </pre>
            </details>
          )}

          <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-subtle pt-4">
            <button
              className="inline-flex items-center gap-1 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white shadow-brand-glow hover:bg-brand-600 transition disabled:cursor-not-allowed disabled:opacity-60"
              disabled
              title="Credential capture UI lands in Phase 1.5"
            >
              <Plus size={12} />
              Connect (Phase 1.5)
            </button>
            {connector.signup_url && (
              <a
                href={connector.signup_url}
                target="_blank"
                rel="noopener"
                className="inline-flex items-center gap-1 rounded-md border border-default bg-surface px-3 py-1.5 text-xs text-ink-secondary hover:bg-elevated"
              >
                {connector.signup_label ?? "Get credentials"}
                <ExternalLink size={11} />
              </a>
            )}
            {connector.doc_url && (
              <a
                href={connector.doc_url}
                target="_blank"
                rel="noopener"
                className="inline-flex items-center gap-1 text-[11px] text-ink-tertiary hover:text-ink-primary"
              >
                Docs <ExternalLink size={10} />
              </a>
            )}
          </div>
        </div>
      </div>
    </Card>
  );
}

function UpcomingConnectorCard({ connector }: { connector: Connector }) {
  const Icon = CATEGORY_ICON[connector.category];
  return (
    <Card className="opacity-70">
      <div className="flex items-start gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-elevated text-ink-tertiary ring-1 ring-inset ring-strong">
          <Icon size={15} />
        </div>
        <div className="flex-1">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold">{connector.name}</h3>
            <StatusChip tone="ink">Roadmap</StatusChip>
          </div>
          <p className="mt-0.5 text-[10px] uppercase tracking-wider text-ink-tertiary">
            {connector.category} · {connector.monthly_cost}
          </p>
          <p className="mt-2 text-xs leading-relaxed text-ink-secondary">
            {connector.description}
          </p>
        </div>
      </div>
    </Card>
  );
}

function StatusBadge({ status }: { status?: ConnectorStatus }) {
  if (!status) {
    return (
      <div className="inline-flex items-center gap-1.5 text-[11px] text-ink-tertiary">
        <Clock size={11} />
        Checking
      </div>
    );
  }
  if (status.state === "offline") {
    return (
      <div className="inline-flex items-center gap-1.5 text-[11px] text-debit" title={status.message}>
        <XCircle size={11} />
        Offline
      </div>
    );
  }
  if (status.configured) {
    return (
      <div className="inline-flex items-center gap-1.5 text-[11px] text-credit">
        <CheckCircle2 size={11} />
        Online · configured
      </div>
    );
  }
  return (
    <div className="inline-flex items-center gap-1.5 text-[11px] text-pending">
      <div className={cn("h-1.5 w-1.5 rounded-full bg-pending animate-pulse-soft")} />
      Online · awaiting credentials
    </div>
  );
}

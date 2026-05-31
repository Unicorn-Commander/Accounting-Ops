import { CheckCircle2, Clock, AlertTriangle, RefreshCw } from "lucide-react";

import { ConnectEbayButton } from "@/components/connect-ebay-button";
import { type EbayConnectorStatus } from "@/lib/ebay-connector";

function formatRelative(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  const diff = Date.now() - d.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours} hr ago`;
  const days = Math.floor(hours / 24);
  return `${days} d ago`;
}

export function EbayStatusCard({
  status,
  workspaceId,
  entityId,
  defaultEntityId
}: {
  status: EbayConnectorStatus | null;
  workspaceId: string;
  entityId: string | null;
  defaultEntityId: string | null;
}) {
  // entityId is the active entity from the EntitySelector. If the connector
  // is already bound to a different entity, we still let the user disconnect
  // from this UI; new connects use the active entity.
  const targetEntity = entityId ?? defaultEntityId;

  if (!status || !status.configured) {
    return (
      <div className="mt-3 rounded-md border border-subtle bg-canvas/40 p-3 text-[12px] text-ink-secondary">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Clock size={13} className="text-ink-tertiary" />
            <span>Not connected. Click Connect to grant sell.finances scope.</span>
          </div>
          <ConnectEbayButton
            workspaceId={workspaceId}
            entityId={targetEntity}
            configured={false}
            sellerHandle={null}
          />
        </div>
      </div>
    );
  }

  const seller = status.seller_handle || "(eBay seller)";
  const lastSync = formatRelative(status.last_sync_at_iso);
  const lastOutcome = status.last_sync_outcome || "—";
  const lastOutcomeOk = lastOutcome === "ok";
  const accessExpires = status.access_token_expires_at_iso
    ? new Date(status.access_token_expires_at_iso)
    : null;
  const refreshExpires = status.refresh_token_expires_at_iso
    ? new Date(status.refresh_token_expires_at_iso)
    : null;
  const accessExpired = accessExpires ? accessExpires.getTime() < Date.now() : false;

  return (
    <div className="mt-3 rounded-md border border-subtle bg-canvas/40 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 text-[12px]">
            <CheckCircle2 size={13} className="text-credit" />
            <span className="font-medium">Connected as {seller}</span>
            {status.environment === "sandbox" && (
              <span className="rounded-sm bg-pending/20 px-1 text-[9px] uppercase tracking-wider text-pending">
                sandbox
              </span>
            )}
          </div>
          <div className="mt-1.5 grid grid-cols-1 gap-y-0.5 text-[11px] text-ink-secondary md:grid-cols-2">
            <div className="flex items-center gap-1">
              <RefreshCw size={10} className="text-ink-tertiary" />
              <span>Last sync: {lastSync}</span>
              <span
                className={
                  lastOutcomeOk ? "text-credit" : "text-debit"
                }
                title={`Last outcome: ${lastOutcome}`}
              >
                ({lastOutcome})
              </span>
            </div>
            <div>{status.recent_imports_count} journal entries this month</div>
            <div className="text-ink-tertiary">
              Scopes: {status.granted_scopes.length}
              {status.granted_scopes.some((s) => s.endsWith("/sell.finances")) ? (
                <span className="ml-1 text-credit">· sell.finances ✓</span>
              ) : (
                <span className="ml-1 text-pending">
                  · sell.finances missing — reconnect required
                </span>
              )}
            </div>
            <div className="text-ink-tertiary">
              Access token:{" "}
              {accessExpired ? (
                <span className="text-debit">expired (auto-refreshes on next call)</span>
              ) : (
                <span>auto-refreshes</span>
              )}
            </div>
            {refreshExpires && (
              <div className="text-ink-tertiary">
                Refresh token valid until {refreshExpires.toISOString().slice(0, 10)}
              </div>
            )}
            {status.entity_id && status.entity_id !== targetEntity && (
              <div className="col-span-full text-pending">
                <AlertTriangle size={10} className="mr-0.5 inline" />
                Connector is bound to a different entity than the active selector. Switch entities to manage it.
              </div>
            )}
          </div>
        </div>
        <ConnectEbayButton
          workspaceId={workspaceId}
          entityId={targetEntity}
          configured={true}
          sellerHandle={status.seller_handle}
        />
      </div>
    </div>
  );
}

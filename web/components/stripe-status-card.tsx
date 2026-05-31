"use client";

import { CheckCircle2, Clock, AlertTriangle, RefreshCw, Plus, Loader2, Unlink, Webhook } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { cn } from "@/lib/cn";

import { ConnectStripeModal } from "@/components/connect-stripe-modal";
import { type StripeConnectorStatus } from "@/lib/stripe-connector";

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

export function StripeStatusCard({
  status,
  workspaceId,
  workspaceName,
  entityId,
  defaultEntityId
}: {
  status: StripeConnectorStatus | null;
  workspaceId: string;
  workspaceName: string;
  entityId: string | null;
  defaultEntityId: string | null;
}) {
  const router = useRouter();
  const targetEntity = entityId ?? defaultEntityId;
  const [disconnecting, setDisconnecting] = useState(false);
  const [showWebhookForm, setShowWebhookForm] = useState(false);
  const [webhookSecret, setWebhookSecret] = useState("");
  const [webhookSaving, setWebhookSaving] = useState(false);
  const [webhookError, setWebhookError] = useState<string | null>(null);

  if (!status || !status.configured) {
    return (
      <div className="mt-3 rounded-md border border-subtle bg-canvas/40 p-3 text-[12px] text-ink-secondary">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Clock size={13} className="text-ink-tertiary" />
            <span>Not connected. Click Connect to paste a restricted API key.</span>
          </div>
          <ConnectStripeModal
            workspaceId={workspaceId}
            workspaceName={workspaceName}
            entityId={targetEntity}
            defaultEntityId={defaultEntityId}
            trigger={
              <button className="inline-flex items-center gap-1 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600 transition">
                <Plus size={12} />
                Connect Stripe
              </button>
            }
          />
        </div>
      </div>
    );
  }

  const businessName = status.business_name || "(Stripe account)";
  const lastSync = formatRelative(status.last_sync_at_iso);
  const lastOutcome = status.last_sync_outcome || "—";
  const lastOutcomeOk = lastOutcome === "ok";

  async function disconnect() {
    if (
      !confirm(
        `Disconnect Stripe (${businessName})? Existing journal entries are not deleted; the credential is wiped so the sync worker stops pulling.`
      )
    ) {
      return;
    }
    setDisconnecting(true);
    try {
      const res = await fetch(
        `/api/proxy/api/workspaces/${workspaceId}/connectors/stripe`,
        { method: "DELETE" }
      );
      if (res.ok || res.status === 204) {
        router.refresh();
      } else {
        const body = await res.text();
        alert(`Disconnect failed: ${body || res.status}`);
      }
    } catch (err) {
      alert(`Disconnect failed: ${err instanceof Error ? err.message : "unknown"}`);
    } finally {
      setDisconnecting(false);
    }
  }

  async function saveWebhookSecret() {
    setWebhookError(null);
    setWebhookSaving(true);
    try {
      const res = await fetch(
        `/api/workspaces/${workspaceId}/connectors/stripe/webhook-secret`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ webhook_secret: webhookSecret.trim() })
        }
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(body.detail ?? "Save failed");
      }
      setShowWebhookForm(false);
      setWebhookSecret("");
      router.refresh();
    } catch (e) {
      setWebhookError(e instanceof Error ? e.message : "Unknown error");
    } finally {
      setWebhookSaving(false);
    }
  }

  return (
    <div className="mt-3 rounded-md border border-subtle bg-canvas/40 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 text-[12px]">
            <CheckCircle2 size={13} className="text-credit" />
            <span className="font-medium">Connected to {businessName}</span>
            {status.environment === "test" && (
              <span className="rounded-sm bg-pending/20 px-1 text-[9px] uppercase tracking-wider text-pending">
                test mode
              </span>
            )}
          </div>
          <div className="mt-1.5 grid grid-cols-1 gap-y-0.5 text-[11px] text-ink-secondary md:grid-cols-2">
            <div className="flex items-center gap-1">
              <RefreshCw size={10} className="text-ink-tertiary" />
              <span>Last sync: {lastSync}</span>
              <span
                className={lastOutcomeOk ? "text-credit" : "text-debit"}
                title={`Last outcome: ${lastOutcome}`}
              >
                ({lastOutcome})
              </span>
            </div>
            <div>{status.recent_imports_count} journal entries this month</div>
            <div className="text-ink-tertiary font-mono">{status.key_display}</div>
            <div className="text-ink-tertiary">
              {status.account_id ? `acct: ${status.account_id.slice(0, 16)}…` : ""}
            </div>
            <div className="text-ink-tertiary">
              Default revenue acct: <span className="font-mono">{status.default_revenue_account}</span>
            </div>
            <div className="flex items-center gap-1 text-ink-tertiary">
              <Webhook size={10} />
              {status.webhook_secret_configured ? (
                <span className="text-credit">webhook configured</span>
              ) : (
                <span className="text-pending">webhook secret missing</span>
              )}
            </div>
            {status.entity_id && status.entity_id !== targetEntity && (
              <div className="col-span-full text-pending">
                <AlertTriangle size={10} className="mr-0.5 inline" />
                Connector is bound to a different entity than the active selector.
              </div>
            )}
            {status.warnings.map((w) => (
              <div key={w} className="col-span-full text-pending">
                <AlertTriangle size={10} className="mr-0.5 inline" />
                {w}
              </div>
            ))}
          </div>

          {!status.webhook_secret_configured && (
            <div className="mt-3 rounded-md border border-subtle bg-canvas/60 p-2">
              {!showWebhookForm ? (
                <button
                  onClick={() => setShowWebhookForm(true)}
                  className="inline-flex items-center gap-1 text-[11px] text-brand-400 hover:text-brand-300"
                >
                  <Webhook size={11} />
                  Add webhook signing secret
                </button>
              ) : (
                <div className="space-y-2">
                  <p className="text-[11px] text-ink-secondary">
                    Register the webhook endpoint in Stripe at{" "}
                    <a
                      href="https://dashboard.stripe.com/webhooks"
                      target="_blank"
                      rel="noopener"
                      className="text-brand-400 hover:underline"
                    >
                      dashboard.stripe.com/webhooks
                    </a>{" "}
                    pointing at{" "}
                    <code className="rounded bg-elevated px-1 font-mono text-[10px]">
                      https://accounting.magicunicorn.dev/api/connectors/stripe/webhook
                    </code>
                    , then paste the signing secret here:
                  </p>
                  <input
                    type="password"
                    value={webhookSecret}
                    onChange={(e) => setWebhookSecret(e.target.value)}
                    placeholder="whsec_..."
                    className="w-full rounded-md border border-default bg-canvas px-2 py-1.5 font-mono text-xs focus:border-brand-500 focus:outline-none"
                  />
                  {webhookError && (
                    <div className="rounded border border-debit/40 bg-debit/[0.06] px-2 py-1 text-[11px] text-debit">
                      {webhookError}
                    </div>
                  )}
                  <div className="flex items-center gap-2">
                    <button
                      onClick={saveWebhookSecret}
                      disabled={webhookSaving || webhookSecret.trim().length < 10}
                      className={cn(
                        "inline-flex items-center gap-1 rounded-md bg-brand-500 px-2 py-1 text-[11px] font-medium text-white hover:bg-brand-600",
                        (webhookSaving || webhookSecret.trim().length < 10) &&
                          "cursor-not-allowed opacity-60"
                      )}
                    >
                      {webhookSaving ? <Loader2 size={10} className="animate-spin" /> : null}
                      {webhookSaving ? "Saving..." : "Save secret"}
                    </button>
                    <button
                      onClick={() => {
                        setShowWebhookForm(false);
                        setWebhookSecret("");
                        setWebhookError(null);
                      }}
                      className="rounded-md border border-strong bg-surface px-2 py-1 text-[11px] text-ink-secondary hover:bg-elevated"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
        <button
          onClick={disconnect}
          disabled={disconnecting}
          className={cn(
            "inline-flex items-center gap-1 rounded-md border border-default bg-surface px-3 py-1.5 text-xs text-ink-secondary hover:bg-elevated hover:text-debit transition disabled:cursor-not-allowed disabled:opacity-60"
          )}
        >
          {disconnecting ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <Unlink size={12} />
          )}
          {disconnecting ? "Disconnecting…" : "Disconnect"}
        </button>
      </div>
    </div>
  );
}

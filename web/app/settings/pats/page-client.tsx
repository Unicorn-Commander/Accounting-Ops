"use client";

import { useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  Copy,
  KeyRound,
  Loader2,
  Plus,
  Trash2,
  X
} from "lucide-react";
import * as Dialog from "@radix-ui/react-dialog";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import type {
  PATMeta,
  PersonalAccessToken
} from "@/lib/personal-access-tokens";

type CreateResponse = {
  id: string;
  name: string;
  secret: string;
  prefix: string;
  scopes: string[];
  workspace_scope: string[] | null;
  expires_at: string | null;
  created_at: string;
};

function formatDate(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit"
  });
}

function formatRelative(value: string | null): string {
  if (!value) return "Never";
  const d = new Date(value).getTime();
  if (Number.isNaN(d)) return value;
  const now = Date.now();
  const diff = now - d;
  const min = 60_000;
  const hr = 60 * min;
  const day = 24 * hr;
  if (diff < min) return "Just now";
  if (diff < hr) return `${Math.floor(diff / min)}m ago`;
  if (diff < day) return `${Math.floor(diff / hr)}h ago`;
  if (diff < 30 * day) return `${Math.floor(diff / day)}d ago`;
  return formatDate(value);
}

function maskPrefix(prefix: string): string {
  return `${prefix}…•••`;
}

async function readError(res: Response): Promise<string> {
  const d = await res.json().catch(() => null);
  if (d?.detail) return String(d.detail);
  if (d?.error) return String(d.error);
  return `${res.status} ${res.statusText}`;
}

export function PATsClient({
  initialTokens,
  meta
}: {
  initialTokens: PersonalAccessToken[];
  meta: PATMeta;
}) {
  const [tokens, setTokens] = useState<PersonalAccessToken[]>(initialTokens);
  const [createOpen, setCreateOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState<PersonalAccessToken | null>(
    null
  );

  const activeCount = useMemo(
    () => tokens.filter((t) => t.state === "active").length,
    [tokens]
  );
  const atLimit = activeCount >= meta.max_active_pats;

  async function refetch() {
    try {
      const res = await fetch("/api/me/personal-access-tokens", {
        cache: "no-store"
      });
      if (res.ok) {
        const data = (await res.json()) as PersonalAccessToken[];
        setTokens(data);
      }
    } catch {
      /* swallow */
    }
  }

  async function revoke(t: PersonalAccessToken) {
    setRevokingId(t.id);
    setError(null);
    try {
      const res = await fetch(
        `/api/me/personal-access-tokens/${t.id}/revoke`,
        { method: "POST" }
      );
      if (!res.ok) throw new Error(await readError(res));
      setConfirmRevoke(null);
      await refetch();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRevokingId(null);
    }
  }

  return (
    <div className="space-y-6">
      {/* Header actions */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-sm text-ink-secondary">
          {activeCount} active{" "}
          <span className="text-ink-tertiary">
            of {meta.max_active_pats} allowed
          </span>
        </p>
        <button
          type="button"
          onClick={() => {
            setError(null);
            setCreateOpen(true);
          }}
          disabled={atLimit}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white shadow-brand-glow transition hover:bg-brand-600",
            atLimit && "cursor-not-allowed opacity-50"
          )}
        >
          <Plus size={14} />
          Generate new token
        </button>
      </div>

      {error && (
        <div className="rounded-md border border-debit/40 bg-debit/[0.06] px-3 py-2 text-sm text-debit">
          {error}
        </div>
      )}

      {atLimit && (
        <div className="rounded-md border border-pending/40 bg-pending/[0.06] px-3 py-2 text-sm text-pending">
          You have reached the per-user token limit. Revoke an unused token
          before generating another.
        </div>
      )}

      {/* Token list */}
      {tokens.length === 0 ? (
        <Card>
          <div className="flex flex-col items-center gap-3 py-8 text-center">
            <KeyRound size={28} className="text-ink-tertiary" />
            <p className="text-sm text-ink-secondary">
              No tokens yet. Generate one to connect Claude Desktop, Cursor, or
              any MCP client.
            </p>
          </div>
        </Card>
      ) : (
        <Card padded={false}>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-sm">
              <thead className="sticky top-0 z-10 bg-surface border-b border-subtle text-[10px] uppercase tracking-wider text-ink-tertiary">
                <tr>
                  <th className="px-4 py-3 font-medium">Name</th>
                  <th className="px-4 py-3 font-medium">Prefix</th>
                  <th className="px-4 py-3 font-medium">Scopes</th>
                  <th className="px-4 py-3 font-medium">Workspace</th>
                  <th className="px-4 py-3 font-medium">Created</th>
                  <th className="px-4 py-3 font-medium">Last used</th>
                  <th className="px-4 py-3 font-medium">Expires</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium" />
                </tr>
              </thead>
              <tbody>
                {tokens.map((t) => {
                  const wsLabel = !t.workspace_scope
                    ? "All"
                    : t.workspace_scope
                        .map(
                          (id) =>
                            meta.workspaces.find((w) => w.id === id)
                              ?.display_name ?? id.slice(0, 8)
                        )
                        .join(", ");
                  return (
                    <tr
                      key={t.id}
                      className="border-b border-subtle/50 last:border-0 hover:bg-white/[0.02]"
                    >
                      <td className="px-4 py-3 font-medium">{t.name}</td>
                      <td className="px-4 py-3 font-mono text-xs text-ink-tertiary">
                        {maskPrefix(t.prefix)}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex flex-wrap gap-1">
                          {t.scopes.length === 0 ? (
                            <span className="text-xs text-ink-tertiary">—</span>
                          ) : (
                            t.scopes.map((s) => (
                              <span
                                key={s}
                                className="inline-flex items-center rounded-full bg-white/5 px-2 py-0.5 text-[10px] font-medium text-ink-secondary"
                              >
                                {s}
                              </span>
                            ))
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-xs text-ink-secondary">
                        {wsLabel}
                      </td>
                      <td className="px-4 py-3 text-xs text-ink-secondary">
                        {formatDate(t.created_at)}
                      </td>
                      <td className="px-4 py-3 text-xs text-ink-secondary">
                        {formatRelative(t.last_used_at)}
                      </td>
                      <td className="px-4 py-3 text-xs text-ink-secondary">
                        {t.expires_at ? formatDate(t.expires_at) : "Never"}
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge state={t.state} />
                      </td>
                      <td className="px-4 py-3 text-right">
                        {t.state === "active" ? (
                          <button
                            type="button"
                            onClick={() => setConfirmRevoke(t)}
                            className="inline-flex items-center gap-1 rounded-md border border-default px-2.5 py-1 text-xs text-ink-secondary hover:border-debit/60 hover:text-debit"
                          >
                            <Trash2 size={12} />
                            Revoke
                          </button>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* Create modal */}
      <CreateModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        meta={meta}
        onCreated={async () => {
          await refetch();
        }}
      />

      {/* Revoke confirm */}
      <Dialog.Root
        open={confirmRevoke !== null}
        onOpenChange={(open) => !open && setConfirmRevoke(null)}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-canvas/80 backdrop-blur-sm" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg border border-default bg-surface shadow-card focus:outline-none">
            <div className="border-b border-subtle px-6 py-4">
              <Dialog.Title className="text-base font-semibold">
                Revoke "{confirmRevoke?.name}"?
              </Dialog.Title>
            </div>
            <div className="px-6 py-5 text-sm text-ink-secondary">
              Apps using this token will stop working immediately. Anyone
              using the secret will get 401 on their next request. This cannot
              be undone.
            </div>
            <div className="flex items-center justify-end gap-2 border-t border-subtle px-6 py-4">
              <Dialog.Close className="rounded-md border border-strong bg-surface px-4 py-2 text-sm text-ink-primary hover:bg-elevated">
                Cancel
              </Dialog.Close>
              <button
                type="button"
                onClick={() => confirmRevoke && revoke(confirmRevoke)}
                disabled={revokingId !== null}
                className="inline-flex items-center gap-2 rounded-md bg-debit px-4 py-2 text-sm font-medium text-white transition hover:bg-debit/85 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {revokingId !== null ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : null}
                Revoke token
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}

function StatusBadge({ state }: { state: PersonalAccessToken["state"] }) {
  const map: Record<PersonalAccessToken["state"], string> = {
    active: "bg-credit/15 text-credit",
    expired: "bg-pending/15 text-pending",
    revoked: "bg-blocked/15 text-blocked"
  };
  const label: Record<PersonalAccessToken["state"], string> = {
    active: "Active",
    expired: "Expired",
    revoked: "Revoked"
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        map[state]
      )}
    >
      {label[state]}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Create modal
// ---------------------------------------------------------------------------

function CreateModal({
  open,
  onClose,
  meta,
  onCreated
}: {
  open: boolean;
  onClose: () => void;
  meta: PATMeta;
  onCreated: () => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<Set<string>>(
    new Set(meta.owner_allowed_scopes)
  );
  const [wsScope, setWsScope] = useState<Set<string>>(new Set());
  const [ttl, setTtl] = useState<number>(meta.default_ttl_days);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreateResponse | null>(null);
  const [copied, setCopied] = useState(false);

  function reset() {
    setName("");
    setScopes(new Set(meta.owner_allowed_scopes));
    setWsScope(new Set());
    setTtl(meta.default_ttl_days);
    setSubmitting(false);
    setError(null);
    setCreated(null);
    setCopied(false);
  }

  async function submit() {
    if (!name.trim()) {
      setError("Name is required.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const body: Record<string, unknown> = {
        name: name.trim(),
        ttl_days: ttl
      };
      if (scopes.size > 0 && scopes.size < meta.owner_allowed_scopes.length) {
        body.scopes = Array.from(scopes);
      }
      if (wsScope.size > 0) {
        body.workspace_scope = Array.from(wsScope);
      }
      const res = await fetch("/api/me/personal-access-tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (!res.ok) throw new Error(await readError(res));
      const data = (await res.json()) as CreateResponse;
      setCreated(data);
      await onCreated();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  async function copyToClipboard(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Clipboard write failed — copy manually.");
    }
  }

  function close() {
    onClose();
    // Defer reset so the modal animates out cleanly.
    setTimeout(reset, 200);
  }

  const ttlChoices: { value: number; label: string }[] = meta.ttl_choices_days.map(
    (d) => ({
      value: d,
      label: d === 0 ? "Never" : d === 365 ? "1 year" : `${d} days`
    })
  );

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(o) => {
        if (!o) close();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-canvas/80 backdrop-blur-sm" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 max-h-[90vh] w-full max-w-2xl -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border border-default bg-surface shadow-card focus:outline-none">
          <div className="flex items-center justify-between border-b border-subtle px-6 py-4">
            <Dialog.Title className="text-base font-semibold">
              {created ? "Token created" : "Generate personal access token"}
            </Dialog.Title>
            <Dialog.Close
              onClick={close}
              className="rounded p-1 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
            >
              <X size={16} />
            </Dialog.Close>
          </div>

          {created ? (
            <SecretRevealView
              created={created}
              copied={copied}
              onCopy={copyToClipboard}
              onDone={close}
            />
          ) : (
            <div className="space-y-5 px-6 py-5">
              <label className="block">
                <span className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Name
                </span>
                <input
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. Claude Desktop — laptop"
                  maxLength={100}
                  className="mt-1.5 w-full rounded-md border border-default bg-canvas px-3 py-2 text-sm focus:border-brand-500 focus:outline-none"
                />
              </label>

              <fieldset>
                <legend className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Scopes
                </legend>
                <p className="mt-1.5 text-xs text-ink-tertiary">
                  Limited to what your workspace role allows. Unchecking a
                  scope reduces the token's permissions; you cannot grant
                  scopes you don't have.
                </p>
                <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
                  {meta.all_scopes.map((s) => {
                    const allowed = meta.owner_allowed_scopes.includes(s);
                    const checked = scopes.has(s);
                    return (
                      <label
                        key={s}
                        className={cn(
                          "flex items-center gap-2 rounded-md border px-3 py-2 text-sm",
                          allowed
                            ? "border-default bg-canvas hover:border-strong"
                            : "border-subtle bg-white/[0.02] text-ink-tertiary cursor-not-allowed"
                        )}
                      >
                        <input
                          type="checkbox"
                          disabled={!allowed}
                          checked={checked}
                          onChange={(e) =>
                            setScopes((prev) => {
                              const next = new Set(prev);
                              if (e.target.checked) next.add(s);
                              else next.delete(s);
                              return next;
                            })
                          }
                        />
                        <span className="font-mono text-xs">{s}</span>
                      </label>
                    );
                  })}
                </div>
              </fieldset>

              <fieldset>
                <legend className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Workspace scope
                </legend>
                <p className="mt-1.5 text-xs text-ink-tertiary">
                  Leave empty for all workspaces you belong to. Select one or
                  more to restrict.
                </p>
                <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
                  {meta.workspaces.length === 0 ? (
                    <p className="text-sm text-ink-tertiary">
                      No workspaces.
                    </p>
                  ) : (
                    meta.workspaces.map((w) => {
                      const checked = wsScope.has(w.id);
                      return (
                        <label
                          key={w.id}
                          className="flex items-center justify-between gap-2 rounded-md border border-default bg-canvas px-3 py-2 text-sm hover:border-strong"
                        >
                          <span className="flex items-center gap-2">
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={(e) =>
                                setWsScope((prev) => {
                                  const next = new Set(prev);
                                  if (e.target.checked) next.add(w.id);
                                  else next.delete(w.id);
                                  return next;
                                })
                              }
                            />
                            <span>{w.display_name}</span>
                          </span>
                          <span className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                            {w.role}
                          </span>
                        </label>
                      );
                    })
                  )}
                </div>
              </fieldset>

              <fieldset>
                <legend className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Expires
                </legend>
                <div className="mt-3 flex flex-wrap gap-2">
                  {ttlChoices.map((c) => (
                    <label
                      key={c.value}
                      className={cn(
                        "inline-flex cursor-pointer items-center gap-2 rounded-md border px-3 py-1.5 text-sm",
                        ttl === c.value
                          ? "border-brand-500 bg-brand-500/10 text-brand-300"
                          : "border-default bg-canvas hover:border-strong"
                      )}
                    >
                      <input
                        type="radio"
                        name="ttl"
                        value={c.value}
                        checked={ttl === c.value}
                        onChange={() => setTtl(c.value)}
                        className="sr-only"
                      />
                      {c.label}
                    </label>
                  ))}
                </div>
              </fieldset>

              {error && (
                <div className="rounded-md border border-debit/40 bg-debit/[0.06] px-3 py-2 text-sm text-debit">
                  {error}
                </div>
              )}

              <div className="flex items-center justify-end gap-2 border-t border-subtle pt-4">
                <button
                  type="button"
                  onClick={close}
                  className="rounded-md border border-strong bg-surface px-4 py-2 text-sm text-ink-primary hover:bg-elevated"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={submit}
                  disabled={submitting || !name.trim()}
                  className={cn(
                    "inline-flex items-center gap-2 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white shadow-brand-glow transition hover:bg-brand-600",
                    (submitting || !name.trim()) &&
                      "cursor-not-allowed opacity-60"
                  )}
                >
                  {submitting ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : null}
                  {submitting ? "Creating..." : "Generate token"}
                </button>
              </div>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function SecretRevealView({
  created,
  copied,
  onCopy,
  onDone
}: {
  created: CreateResponse;
  copied: boolean;
  onCopy: (text: string) => Promise<void>;
  onDone: () => void;
}) {
  const claudeConfig = useMemo(
    () =>
      JSON.stringify(
        {
          mcpServers: {
            "accounting-ops": {
              url: "https://accounting-ops.magicunicorn.dev/mcp",
              transport: "streamable_http",
              headers: {
                Authorization: `Bearer ${created.secret}`
              }
            }
          }
        },
        null,
        2
      ),
    [created.secret]
  );

  return (
    <div className="space-y-5 px-6 py-5">
      <div className="flex items-start gap-3 rounded-md border border-debit/40 bg-debit/[0.06] px-4 py-3">
        <AlertTriangle size={18} className="mt-0.5 shrink-0 text-debit" />
        <div className="text-sm">
          <p className="font-semibold text-ink-primary">
            Save this token now.
          </p>
          <p className="mt-1 text-ink-secondary">
            We will never show it again. If you lose it, you will need to
            generate a new one.
          </p>
        </div>
      </div>

      <div>
        <p className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
          Your new token
        </p>
        <div className="mt-1.5 flex items-stretch gap-2">
          <pre className="flex-1 overflow-x-auto rounded-md border border-default bg-canvas px-3 py-2.5 font-mono text-xs text-ink-primary">
            {created.secret}
          </pre>
          <button
            type="button"
            onClick={() => onCopy(created.secret)}
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 rounded-md border border-default bg-surface px-3 py-2 text-xs hover:border-strong",
              copied && "border-credit/60 text-credit"
            )}
          >
            {copied ? <Check size={12} /> : <Copy size={12} />}
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      </div>

      <div>
        <div className="flex items-center justify-between">
          <p className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
            Claude Desktop config snippet
          </p>
          <button
            type="button"
            onClick={() => onCopy(claudeConfig)}
            className="inline-flex items-center gap-1.5 rounded-md border border-default bg-surface px-2.5 py-1 text-xs hover:border-strong"
          >
            <Copy size={12} />
            Copy JSON
          </button>
        </div>
        <p className="mt-1.5 text-xs text-ink-tertiary">
          Add this block to{" "}
          <code className="font-mono text-[11px]">
            ~/Library/Application Support/Claude/claude_desktop_config.json
          </code>{" "}
          and restart Claude Desktop.
        </p>
        <pre className="mt-2 overflow-x-auto rounded-md border border-default bg-canvas px-3 py-2.5 font-mono text-[11px] text-ink-secondary">
          {claudeConfig}
        </pre>
      </div>

      <div className="flex items-center justify-end border-t border-subtle pt-4">
        <button
          type="button"
          onClick={onDone}
          className="inline-flex items-center gap-1 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white shadow-brand-glow hover:bg-brand-600"
        >
          I've saved it
        </button>
      </div>
    </div>
  );
}

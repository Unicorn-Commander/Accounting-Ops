"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  Building2,
  Check,
  Layers,
  Mail,
  Plus,
  RotateCw,
  Trash2,
  User,
  XCircle,
} from "lucide-react";
import type {
  Entity,
  Invitation,
  Member,
  WorkspaceRole,
} from "@/lib/api";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";

// Roles offered in the role dropdown. platform_admin is intentionally not
// invite-able through this UI; the API rejects it server-side too.
const ROLE_OPTIONS: { value: WorkspaceRole; label: string; hint?: string }[] = [
  { value: "viewer", label: "Viewer", hint: "Read-only" },
  {
    value: "client_user",
    label: "Team member",
    hint: "Read + answer decisions + upload",
  },
  {
    value: "bookkeeper",
    label: "Bookkeeper",
    hint: "Propose categorizations (cannot approve)",
  },
  {
    value: "accountant",
    label: "Accountant",
    hint: "Write all books, decide approvals",
  },
  {
    value: "cpa",
    label: "External CPA",
    hint: "Same as accountant; tagged as outside CPA",
  },
  { value: "admin", label: "Admin", hint: "Manage members + COA (owner-only invite)" },
  {
    value: "owner",
    label: "Owner",
    hint: "Full control (owner-only invite)",
  },
];

const ELEVATED: WorkspaceRole[] = ["admin", "owner", "client_owner"];

function fmtDate(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return s;
  }
}

// ---------------------------------------------------------------------------
// H11: Confirmation dialog — used in place of native confirm() for member
// removal + invitation cancellation. Same modal pattern as InviteModal: a
// fixed overlay with a card, backdrop-click closes, focus trap is implicit
// because the only interactive elements are the two buttons.
// ---------------------------------------------------------------------------

function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel = "Cancel",
  destructive = true,
  busy,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  description: React.ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  destructive?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/80 backdrop-blur-sm px-4"
      onClick={onCancel}
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-dialog-title"
    >
      <div
        className="w-full max-w-md rounded-lg border border-default bg-surface p-5 shadow-card"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-3">
          <AlertTriangle
            size={18}
            className={cn(
              "mt-0.5 shrink-0",
              destructive ? "text-debit" : "text-pending"
            )}
            aria-hidden="true"
          />
          <div className="min-w-0 flex-1">
            <h3 id="confirm-dialog-title" className="text-base font-semibold">
              {title}
            </h3>
            <div className="mt-1 text-xs text-ink-secondary">{description}</div>
          </div>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded-md border border-subtle px-3 py-1.5 text-sm text-ink-secondary hover:bg-white/[0.04] disabled:opacity-50"
            autoFocus
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50",
              destructive ? "bg-debit hover:bg-debit/90" : "bg-brand-500 hover:bg-brand-600"
            )}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

type ConfirmPayload =
  | {
      kind: "remove-member";
      label: string;
      userId: string;
    }
  | {
      kind: "cancel-invite";
      label: string;
      inviteId: string;
    };

export function MembersClient({
  workspaceId,
  workspaceName,
  initialMembers,
  initialInvitations,
  entities,
  canOwner,
}: {
  workspaceId: string;
  workspaceName: string;
  initialMembers: Member[];
  initialInvitations: Invitation[];
  entities: Entity[];
  canOwner: boolean;
}) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [members] = useState(initialMembers);
  const [invitations] = useState(initialInvitations);
  const [inviting, setInviting] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ConfirmPayload | null>(null);

  async function callApi(
    path: string,
    method: string,
    body?: unknown
  ): Promise<{ ok: boolean; status: number; data: unknown }> {
    const res = await fetch(`/api/proxy${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      /* empty body */
    }
    return { ok: res.ok, status: res.status, data };
  }

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <div className="mb-3 flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold">Active members</h2>
            <p className="text-xs text-ink-tertiary">
              {workspaceName} — {members.length} member{members.length === 1 ? "" : "s"}
            </p>
          </div>
          <button
            type="button"
            onClick={() => setInviting(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-600"
          >
            <Plus size={14} /> Invite member
          </button>
        </div>

        {errMsg ? (
          <div className="mb-3 rounded-md border border-debit/30 bg-debit/10 px-3 py-2 text-sm text-debit">
            {errMsg}
          </div>
        ) : null}

        {members.length === 0 ? (
          <p className="text-sm text-ink-tertiary">No members yet.</p>
        ) : (
          <ul className="divide-y divide-subtle">
            {members.map((m) => (
              <li
                key={m.membership_id}
                className="flex flex-col gap-2 py-3 md:flex-row md:items-center md:gap-4"
              >
                <div className="flex min-w-0 flex-1 items-center gap-3">
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-500/15 text-xs font-semibold text-brand-300">
                    {(m.email || m.uc_uid).slice(0, 2).toUpperCase()}
                  </div>
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      {m.display_name || m.email || m.uc_uid}
                    </p>
                    <p className="truncate text-xs text-ink-tertiary">
                      {m.email || `uc_uid:${m.uc_uid}`}
                    </p>
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <RoleBadge role={m.role} />
                  {m.entity_scope && m.entity_scope.length > 0 ? (
                    <span className="rounded-full border border-subtle px-2 py-0.5 text-[10px] text-ink-tertiary">
                      {m.entity_scope.length} entit
                      {m.entity_scope.length === 1 ? "y" : "ies"} scope
                    </span>
                  ) : (
                    <span className="rounded-full border border-subtle px-2 py-0.5 text-[10px] text-ink-tertiary">
                      All entities
                    </span>
                  )}
                  <span className="text-[11px] text-ink-tertiary">
                    Last active {fmtDate(m.last_active_at)}
                  </span>
                  {ELEVATED.includes(m.role) && !canOwner ? null : (
                    <button
                      type="button"
                      disabled={busyId === m.user_id}
                      className="inline-flex items-center gap-1 rounded border border-debit/30 px-2 py-1 text-xs text-debit hover:bg-debit/10 disabled:opacity-50"
                      onClick={() =>
                        // H11: native confirm() replaced with shared modal.
                        // SAFETY: explicit confirm step is required before
                        //         any DELETE — no path bypasses ConfirmDialog.
                        setConfirm({
                          kind: "remove-member",
                          label: m.email || m.uc_uid,
                          userId: m.user_id,
                        })
                      }
                    >
                      <Trash2 size={12} /> Remove
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <div className="mb-3">
          <h2 className="text-sm font-semibold">Pending invitations</h2>
          <p className="text-xs text-ink-tertiary">
            {invitations.length} pending
          </p>
        </div>
        {invitations.length === 0 ? (
          <p className="text-sm text-ink-tertiary">No pending invitations.</p>
        ) : (
          <ul className="divide-y divide-subtle">
            {invitations.map((inv) => (
              <li
                key={inv.id}
                className="flex flex-col gap-2 py-3 md:flex-row md:items-center md:gap-4"
              >
                <div className="flex min-w-0 flex-1 items-center gap-3">
                  <Mail size={14} className="text-ink-tertiary" />
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{inv.email}</p>
                    <p className="truncate text-xs text-ink-tertiary">
                      Invited by {inv.created_by_uc_uid} ·{" "}
                      {fmtDate(inv.created_at)}
                    </p>
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <RoleBadge role={inv.role} />
                  <span className="text-[11px] text-ink-tertiary">
                    Expires {fmtDate(inv.expires_at)}
                  </span>
                  {inv.resent_count > 0 ? (
                    <span className="text-[11px] text-ink-tertiary">
                      Resent {inv.resent_count}x
                    </span>
                  ) : null}
                  <button
                    type="button"
                    disabled={busyId === inv.id}
                    className="inline-flex items-center gap-1 rounded border border-subtle px-2 py-1 text-xs text-ink-secondary hover:bg-white/[0.04] disabled:opacity-50"
                    onClick={async () => {
                      setBusyId(inv.id);
                      setErrMsg(null);
                      const res = await callApi(
                        `/api/workspaces/${workspaceId}/invitations/${inv.id}/resend`,
                        "POST"
                      );
                      setBusyId(null);
                      if (!res.ok) {
                        setErrMsg(
                          (res.data as { detail?: string })?.detail ||
                            `Resend failed (${res.status})`
                        );
                        return;
                      }
                      startTransition(() => router.refresh());
                    }}
                  >
                    <RotateCw size={12} /> Resend
                  </button>
                  <button
                    type="button"
                    disabled={busyId === inv.id}
                    className="inline-flex items-center gap-1 rounded border border-debit/30 px-2 py-1 text-xs text-debit hover:bg-debit/10 disabled:opacity-50"
                    onClick={() =>
                      // H11: native confirm() replaced with shared modal.
                      setConfirm({
                        kind: "cancel-invite",
                        label: inv.email,
                        inviteId: inv.id,
                      })
                    }
                  >
                    <XCircle size={12} /> Cancel
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {inviting ? (
        <InviteModal
          workspaceId={workspaceId}
          entities={entities}
          canOwner={canOwner}
          onClose={(refresh) => {
            setInviting(false);
            if (refresh) startTransition(() => router.refresh());
          }}
        />
      ) : null}

      {/* H11: shared confirmation dialog for member-removal + invite-cancel.
          Replaces the native confirm() that fired raw browser prompts. */}
      <ConfirmDialog
        open={confirm !== null}
        title={
          confirm?.kind === "remove-member"
            ? "Remove member?"
            : "Cancel invitation?"
        }
        description={
          confirm?.kind === "remove-member" ? (
            <>
              <p>
                <span className="text-ink-primary">{confirm?.label}</span> will
                lose access to <span className="text-ink-primary">{workspaceName}</span>{" "}
                immediately.
              </p>
              <p className="mt-2 text-ink-tertiary">
                Their decision history and audit trail stay in place — only
                the active membership is removed.
              </p>
            </>
          ) : (
            <>
              <p>
                The pending invitation to{" "}
                <span className="text-ink-primary">{confirm?.label}</span> will
                be revoked. The magic link in their email stops working
                immediately.
              </p>
              <p className="mt-2 text-ink-tertiary">
                You can issue a new invitation any time.
              </p>
            </>
          )
        }
        confirmLabel={
          confirm?.kind === "remove-member"
            ? "Remove member"
            : "Cancel invitation"
        }
        destructive
        busy={busyId === (confirm?.kind === "remove-member" ? confirm.userId : confirm?.kind === "cancel-invite" ? confirm.inviteId : null)}
        onCancel={() => setConfirm(null)}
        onConfirm={async () => {
          if (!confirm) return;
          setErrMsg(null);
          if (confirm.kind === "remove-member") {
            setBusyId(confirm.userId);
            const res = await callApi(
              `/api/workspaces/${workspaceId}/members/${confirm.userId}`,
              "DELETE"
            );
            setBusyId(null);
            setConfirm(null);
            if (!res.ok) {
              setErrMsg(
                (res.data as { detail?: string })?.detail ||
                  `Remove failed (${res.status})`
              );
              return;
            }
            startTransition(() => router.refresh());
          } else {
            setBusyId(confirm.inviteId);
            const res = await callApi(
              `/api/workspaces/${workspaceId}/invitations/${confirm.inviteId}/cancel`,
              "POST"
            );
            setBusyId(null);
            setConfirm(null);
            if (!res.ok) {
              setErrMsg(
                (res.data as { detail?: string })?.detail ||
                  `Cancel failed (${res.status})`
              );
              return;
            }
            startTransition(() => router.refresh());
          }
        }}
      />
    </div>
  );
}

function RoleBadge({ role }: { role: WorkspaceRole }) {
  const tone: Record<WorkspaceRole, string> = {
    platform_admin: "bg-brand-500/20 text-brand-300 border-brand-500/40",
    owner: "bg-brand-500/15 text-brand-300 border-brand-500/30",
    client_owner: "bg-brand-500/15 text-brand-300 border-brand-500/30",
    admin: "bg-agent-500/15 text-agent-500 border-agent-500/30",
    cpa: "bg-pending/15 text-pending border-pending/30",
    accountant: "bg-pending/15 text-pending border-pending/30",
    bookkeeper: "bg-white/10 text-ink-secondary border-subtle",
    client_user: "bg-white/5 text-ink-secondary border-subtle",
    viewer: "bg-white/5 text-ink-tertiary border-subtle",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        tone[role]
      )}
    >
      {role}
    </span>
  );
}

function InviteModal({
  workspaceId,
  entities,
  canOwner,
  onClose,
}: {
  workspaceId: string;
  entities: Entity[];
  canOwner: boolean;
  onClose: (refresh: boolean) => void;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<WorkspaceRole>("cpa");
  const [scope, setScope] = useState<string[]>([]);
  const [ttl, setTtl] = useState(7);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/80 backdrop-blur-sm"
      onClick={() => onClose(false)}
    >
      <div
        className="w-full max-w-md rounded-lg border border-default bg-surface p-5 shadow-card"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-semibold">Invite member</h3>
        <p className="mt-1 text-xs text-ink-tertiary">
          Sends a magic link to the email below. The invitation expires after
          the TTL you set.
        </p>

        {err ? (
          <div className="mt-3 rounded-md border border-debit/30 bg-debit/10 px-3 py-2 text-sm text-debit">
            {err}
          </div>
        ) : null}

        <div className="mt-4 flex flex-col gap-3">
          <label className="block text-xs font-medium text-ink-secondary">
            Email
            <input
              type="email"
              required
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="cpa@example.com"
              className="mt-1 block w-full rounded-md border border-default bg-elevated px-3 py-2 text-sm"
            />
          </label>

          <label className="block text-xs font-medium text-ink-secondary">
            Role
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as WorkspaceRole)}
              className="mt-1 block w-full rounded-md border border-default bg-elevated px-3 py-2 text-sm"
            >
              {ROLE_OPTIONS.filter(
                (r) => !ELEVATED.includes(r.value) || canOwner
              ).map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label} {r.hint ? `— ${r.hint}` : ""}
                </option>
              ))}
            </select>
          </label>

          {/*
           * H11: <select multiple> replaced with mobile-friendly checkbox
           * cards. iPhone users couldn't multi-select with cmd/ctrl; CPAs
           * found the affordance confusing. Each entity shows its display
           * name + a kind chip so users can confirm scope at a glance.
           */}
          <div>
            <label className="block text-xs font-medium text-ink-secondary">
              Restrict to entities (optional)
            </label>
            <p className="mt-1 text-[10px] text-ink-tertiary">
              Leave all unchecked for full workspace access.
            </p>
            {entities.length === 0 ? (
              <p className="mt-2 rounded-md border border-subtle bg-elevated px-3 py-2 text-xs text-ink-tertiary">
                No entities to choose from yet.
              </p>
            ) : (
              <div className="mt-2 max-h-56 space-y-1.5 overflow-y-auto rounded-md border border-default bg-elevated/40 p-2">
                {entities.map((ent) => {
                  const checked = scope.includes(ent.id);
                  // Choose icon by entity kind for fast visual scan.
                  const kind = (ent as Entity & { kind?: string }).kind ?? "";
                  const KindIcon = kind === "individual"
                    ? User
                    : kind === "consolidated"
                      ? Layers
                      : Building2;
                  return (
                    <label
                      key={ent.id}
                      className={cn(
                        "flex cursor-pointer items-center gap-3 rounded-md border px-3 py-2 transition",
                        checked
                          ? "border-brand-500/60 bg-brand-500/[0.08]"
                          : "border-subtle bg-surface hover:bg-white/[0.04]"
                      )}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(e) => {
                          setScope((prev) =>
                            e.target.checked
                              ? [...prev, ent.id]
                              : prev.filter((x) => x !== ent.id)
                          );
                        }}
                        className="h-4 w-4 shrink-0 accent-brand-500"
                        aria-label={`Include ${ent.display_name}`}
                      />
                      <KindIcon size={14} className="shrink-0 text-ink-tertiary" />
                      <span className="min-w-0 flex-1 truncate text-sm text-ink-primary">
                        {ent.display_name}
                      </span>
                      {kind ? (
                        <span className="shrink-0 rounded-full border border-subtle bg-white/[0.04] px-2 py-0.5 text-[10px] uppercase tracking-wider text-ink-tertiary">
                          {kind.replace(/_/g, " ")}
                        </span>
                      ) : null}
                      {checked ? (
                        <Check size={12} className="shrink-0 text-brand-400" aria-hidden="true" />
                      ) : null}
                    </label>
                  );
                })}
              </div>
            )}
            {scope.length > 0 ? (
              <p className="mt-1.5 text-[10px] text-brand-300">
                {scope.length} entit{scope.length === 1 ? "y" : "ies"} selected
              </p>
            ) : null}
          </div>

          <label className="block text-xs font-medium text-ink-secondary">
            Expires after (days)
            <input
              type="number"
              min={1}
              max={30}
              value={ttl}
              onChange={(e) => setTtl(Math.max(1, Math.min(30, +e.target.value || 7)))}
              className="mt-1 block w-full rounded-md border border-default bg-elevated px-3 py-2 text-sm"
            />
          </label>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={() => onClose(false)}
            className="rounded-md border border-subtle px-3 py-1.5 text-sm text-ink-secondary hover:bg-white/[0.04]"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!email || busy}
            className="rounded-md bg-brand-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-600 disabled:opacity-50"
            onClick={async () => {
              setBusy(true);
              setErr(null);
              const res = await fetch(
                `/api/proxy/api/workspaces/${workspaceId}/invitations`,
                {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    email,
                    role,
                    entity_scope: scope.length > 0 ? scope : null,
                    ttl_days: ttl,
                  }),
                }
              );
              setBusy(false);
              let data: { detail?: string; status?: string } = {};
              try {
                data = await res.json();
              } catch {
                /* ignore */
              }
              if (!res.ok) {
                setErr(data.detail || `Invite failed (${res.status})`);
                return;
              }
              onClose(true);
            }}
          >
            {busy ? "Sending…" : "Send invite"}
          </button>
        </div>
      </div>
    </div>
  );
}

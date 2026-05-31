"use client";

import { useState, useTransition } from "react";
import type { SentinelConfig } from "@/lib/sentinel";

const LEVELS = [
  { value: "off", label: "Off", desc: "Sentinel disabled for this workspace." },
  {
    value: "watch_only",
    label: "Watch only",
    desc: "Runs checks; writes only informational findings. No memos, no flags."
  },
  {
    value: "draft_and_flag",
    label: "Draft & flag (recommended)",
    desc:
      "Drafts memos, flags items for CPA review, sends informational emails, schedules reminders, creates open-items. No auto-decide / auto-file / auto-pay."
  },
  {
    value: "fully_autonomous",
    label: "Fully autonomous",
    desc:
      "Adds: bulk-decide low-risk approvals, initiate connector syncs, set up new entities. Use only for workspaces explicitly opted in."
  }
];

export function SentinelSettingsClient({
  workspaceId,
  initial
}: {
  workspaceId: string;
  initial: SentinelConfig;
}) {
  const [autonomy, setAutonomy] = useState<string>(initial.autonomy_level);
  const [intervalMin, setIntervalMin] = useState<number>(initial.check_interval_minutes);
  const [maxPerDay, setMaxPerDay] = useState<number>(initial.max_actions_per_day);
  const [enabled, setEnabled] = useState<string[]>(initial.enabled_checks);
  const [notifyOwners, setNotifyOwners] = useState<boolean>(initial.notify_owners);
  const [notifyAcct, setNotifyAcct] = useState<boolean>(initial.notify_accountants);
  const [msg, setMsg] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const allChecks = initial.all_check_types;

  function toggleCheck(name: string) {
    setEnabled((prev) =>
      prev.includes(name) ? prev.filter((c) => c !== name) : [...prev, name]
    );
  }

  function saveAll() {
    setMsg(null);
    startTransition(async () => {
      try {
        const resp = await fetch(`/api/web/sentinel/${workspaceId}/config`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            autonomy_level: autonomy,
            check_interval_minutes: intervalMin,
            max_actions_per_day: maxPerDay,
            enabled_checks: enabled.length === allChecks.length ? ["all"] : enabled,
            notify_owners: notifyOwners,
            notify_accountants: notifyAcct
          })
        });
        if (resp.ok) {
          setMsg("Saved.");
        } else {
          const t = await resp.text();
          setMsg(`Failed: ${resp.status} ${t.slice(0, 200)}`);
        }
      } catch (e) {
        setMsg(`Failed: ${String(e).slice(0, 200)}`);
      }
    });
  }

  function runNow() {
    setMsg(null);
    startTransition(async () => {
      try {
        const resp = await fetch(`/api/web/sentinel/${workspaceId}/run`, { method: "POST" });
        if (resp.ok) {
          setMsg("Sentinel cycle queued. Check the Open Items page in a minute.");
        } else {
          const t = await resp.text();
          setMsg(`Failed: ${resp.status} ${t.slice(0, 200)}`);
        }
      } catch (e) {
        setMsg(`Failed: ${String(e).slice(0, 200)}`);
      }
    });
  }

  return (
    <div className="space-y-6">
      {/* Autonomy level */}
      <div>
        <label className="text-[10px] uppercase tracking-wider text-ink-tertiary block mb-2">
          Autonomy level
        </label>
        <div className="space-y-2">
          {LEVELS.map((l) => (
            <label
              key={l.value}
              className="flex items-start gap-2 cursor-pointer rounded-md border border-default/60 p-3 hover:border-strong"
            >
              <input
                type="radio"
                name="autonomy"
                value={l.value}
                checked={autonomy === l.value}
                onChange={() => setAutonomy(l.value)}
                className="mt-1"
              />
              <div className="min-w-0">
                <p className="text-sm font-medium">{l.label}</p>
                <p className="text-xs text-ink-secondary mt-0.5">{l.desc}</p>
              </div>
            </label>
          ))}
        </div>
      </div>

      {/* Cadence + cap */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <label className="block">
          <span className="text-[10px] uppercase tracking-wider text-ink-tertiary">
            Check interval (minutes)
          </span>
          <input
            type="number"
            min={15}
            max={10080}
            value={intervalMin}
            onChange={(e) => setIntervalMin(parseInt(e.target.value || "0") || 360)}
            className="mt-1 block w-full rounded-md border border-default bg-bg-soft px-3 py-2 text-sm"
          />
        </label>
        <label className="block">
          <span className="text-[10px] uppercase tracking-wider text-ink-tertiary">
            Max actions per day (per workspace)
          </span>
          <input
            type="number"
            min={0}
            max={1000}
            value={maxPerDay}
            onChange={(e) => setMaxPerDay(parseInt(e.target.value || "0") || 50)}
            className="mt-1 block w-full rounded-md border border-default bg-bg-soft px-3 py-2 text-sm"
          />
        </label>
      </div>

      {/* Notifications */}
      <div>
        <label className="text-[10px] uppercase tracking-wider text-ink-tertiary block mb-2">
          Notifications
        </label>
        <div className="flex flex-wrap gap-4">
          <label className="inline-flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={notifyOwners}
              onChange={(e) => setNotifyOwners(e.target.checked)}
            />
            Workspace owners
          </label>
          <label className="inline-flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={notifyAcct}
              onChange={(e) => setNotifyAcct(e.target.checked)}
            />
            Accountants (CPAs)
          </label>
        </div>
      </div>

      {/* Enabled checks */}
      <div>
        <label className="text-[10px] uppercase tracking-wider text-ink-tertiary block mb-2">
          Enabled checks
        </label>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
          {allChecks.map((c) => (
            <label
              key={c}
              className="inline-flex items-center gap-2 text-xs rounded-md border border-default/60 px-2 py-1.5"
            >
              <input
                type="checkbox"
                checked={enabled.includes("all") || enabled.includes(c)}
                onChange={() => toggleCheck(c)}
              />
              <span>{c}</span>
            </label>
          ))}
        </div>
      </div>

      {/* Save + run */}
      <div className="flex flex-col sm:flex-row gap-3 items-start sm:items-center">
        <button
          type="button"
          onClick={saveAll}
          disabled={pending}
          className="rounded-md bg-agent-500 px-4 py-2 text-sm font-medium text-bg disabled:opacity-50"
        >
          {pending ? "Saving…" : "Save settings"}
        </button>
        <button
          type="button"
          onClick={runNow}
          disabled={pending || autonomy === "off"}
          className="rounded-md border border-strong px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          Run a cycle now
        </button>
        {msg ? <span className="text-xs text-ink-secondary">{msg}</span> : null}
      </div>
    </div>
  );
}

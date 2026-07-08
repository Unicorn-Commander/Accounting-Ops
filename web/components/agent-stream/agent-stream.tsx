"use client";

import { useEffect } from "react";
import { Shield, Sparkles } from "lucide-react";
import { useAgentStream } from "./use-agent-stream";
import { DecisionCard } from "./decision-card";
import { ThinkingCard } from "./thinking-card";
import type { AgentDecision } from "./types";

export function AgentStream({
  workspaceId,
  entityId,
  initialDecisions = [],
}: {
  workspaceId?: string;
  entityId?: string;
  initialDecisions?: AgentDecision[];
}) {
  const { state, startStream, stopStream, isDemo } = useAgentStream({
    workspaceId,
    entityId,
    initialDecisions,
  });

  // Restart when workspace/entity changes: the cleanup tears down the previous
  // entity's SSE + timers before the new one opens. Without it, an entity switch
  // (router.refresh, no remount) leaves the ticker stuck on the prior entity and
  // leaks its SSE connection.
  useEffect(() => {
    startStream();
    return stopStream;
  }, [startStream, stopStream]);

  const thinkingDecision = state.decisions.find((d) => d.status === "think") ?? null;
  const completedDecisions = state.decisions.filter((d) => d.status !== "think");

  return (
    <div className="card-depth" style={{ padding: 0 }}>
      <div className="streamhead">
        <div className="ti">
          <Sparkles size={17} className="text-[var(--agent-500)]" />
          Agents at work
          <span className="live-pill">
            <span className="d" />
            LIVE
          </span>
        </div>
        <div className="flex items-center gap-3.5">
          <span className="tamper">
            <Shield size={12} />
            append-only · tamper-evident
          </span>
          <a href="/agent-activity" className="link">View all →</a>
        </div>
      </div>

      <div className="statpills">
        <div className="sp">
          <span className="dt think" />
          <span className="n">{state.counts.thinking}</span> thinking
        </div>
        <div className="sp">
          <span className="dt await" />
          <span className="n">{state.counts.awaiting}</span> awaiting you
        </div>
        <div className="sp">
          <span className="dt appr" />
          <span className="n">{state.counts.autoApproved}</span> auto-approved
        </div>
        <div className="sp">
          <span className="dt block" />
          <span className="n">{state.counts.blocked}</span> blocked
        </div>
      </div>

      <div className="stream">
        {thinkingDecision && (
          <ThinkingCard key={thinkingDecision.id} decision={thinkingDecision} />
        )}
        {completedDecisions.map((d) => (
          <DecisionCard key={d.id} decision={d} />
        ))}
        {!isDemo && completedDecisions.length === 0 && !thinkingDecision && (
          <div
            style={{
              padding: "36px 18px",
              textAlign: "center",
              color: "var(--ink-3)",
              fontSize: 12.5,
              lineHeight: 1.6,
            }}
          >
            <Sparkles size={22} className="text-[var(--agent-500)]" style={{ opacity: 0.6, marginBottom: 8 }} />
            <div style={{ fontWeight: 500, color: "var(--ink-2)" }}>No agent activity yet</div>
            <div style={{ marginTop: 4 }}>
              Connect a bank or drop in documents — as your agents categorize,
              reconcile, and flag, their decisions stream in here.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

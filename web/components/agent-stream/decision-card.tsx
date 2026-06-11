import { FileText, Gavel } from "lucide-react";
import type { AgentDecision } from "./types";
import { resolveAgent } from "./agent-config";

export function DecisionCard({ decision: j }: { decision: AgentDecision }) {
  const a = resolveAgent(j.ag);
  const hasConf = typeof j.conf === "number";
  const cc =
    !hasConf
      ? "var(--agent-500)"
      : (j.conf as number) >= 90
        ? "var(--credit)"
        : (j.conf as number) >= 70
          ? "var(--pending)"
          : "var(--debit)";

  const stMap: Record<string, string> = {
    appr: "auto-approved",
    await: "needs you",
    block: "blocked",
  };
  const statusText = j.statusLabel ?? stMap[j.status] ?? j.status;

  return (
    <div className="dec" style={{ "--ag": a.c1 } as React.CSSProperties}>
      <div className="accent" />

      <div
        className="ava"
        style={{ background: `linear-gradient(135deg, ${a.c1}, ${a.c2})` }}
      >
        {a.ini}
      </div>

      <div className="body">
        <div className="ln1">
          <span className="agpill">{j.agentLabel ?? j.ag}</span>
          <span className="ts">{j.timestamp}</span>
        </div>
        <div className="title">{j.title}</div>
        {j.why && <div className="why">{j.why}</div>}

        {(j.diff || j.rule || j.receipt) && (
          <div className="cites">
            {j.diff && (
              <span className="diff">
                <span className="catchip old">{j.diff[0]}</span>
                <span className="arrow">→</span>
                <span className="catchip new">{j.diff[1]}</span>
              </span>
            )}
            {j.rule && (
              <span className="rule">
                <Gavel size={11} opacity={0.8} />
                {j.rule}
              </span>
            )}
            {j.receipt && (
              <span className="receipt">
                <span className="thumb">
                  <FileText size={9} />
                </span>
                {j.receipt}
              </span>
            )}
          </div>
        )}

        {(j.ent || j.matl || j.amt) && (
          <div className="metaline">
            {j.ent && (
              <span className="ent">
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: 2,
                    background: `hsl(${a.h})`,
                    display: "inline-block",
                  }}
                />
                {j.ent}
              </span>
            )}
            {j.matl && <span className={`matl ${j.matl}`}>{j.matl}</span>}
            {j.amt && <span className={`amt ${j.cr ? "cr" : ""}`}>{j.amt}</span>}
          </div>
        )}
      </div>

      <div className="right">
        {hasConf && (
          <div className="ring" style={{ "--p": j.conf, "--c": cc } as React.CSSProperties}>
            <span>{j.conf}</span>
            <small>conf</small>
          </div>
        )}
        <span className={`status ${j.status}`}>{statusText}</span>
        {j.status === "appr" && (
          <div className="qa">
            <button className="ok">Approve</button>
            <button>Skip</button>
          </div>
        )}
        {j.status === "await" && (
          <div className="qa">
            <button className="ok">Review</button>
          </div>
        )}
        {j.status === "block" && (
          <div className="qa">
            <button>Verify</button>
          </div>
        )}
      </div>
    </div>
  );
}

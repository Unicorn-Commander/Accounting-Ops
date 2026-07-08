"use client";

import { useEffect, useRef } from "react";
import type { AgentDecision } from "./types";
import { resolveAgent } from "./agent-config";

export function ThinkingCard({
  decision,
  onTypewriterDone,
}: {
  decision: AgentDecision;
  onTypewriterDone?: () => void;
}) {
  const a = resolveAgent(decision.ag);
  const twRef = useRef<HTMLSpanElement>(null);
  const doneRef = useRef(false);

  useEffect(() => {
    if (doneRef.current) return;
    doneRef.current = true;

    const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const el = twRef.current;
    if (!el) return;

    if (prefersReduced || !decision.think) {
      el.textContent = decision.think ?? "";
      onTypewriterDone?.();
      return;
    }

    const text = decision.think;
    let i = 0;
    const iv = setInterval(() => {
      el.textContent = text.slice(0, ++i);
      if (i >= text.length) {
        clearInterval(iv);
        onTypewriterDone?.();
      }
    }, 26);

    return () => clearInterval(iv);
  }, [decision.think, onTypewriterDone]);

  return (
    <div
      className="dec thinking"
      style={{ "--ag": a.c1 } as React.CSSProperties}
    >
      <div className="accent" />
      <div
        className="ava"
        style={{
          background: `linear-gradient(135deg, ${a.c1}, ${a.c2})`,
          boxShadow: "var(--glow-agent)",
        }}
      >
        {a.ini}
      </div>
      <div className="body">
        <div className="ln1">
          <span className="agpill">{decision.ag}</span>
          <span className="ts">now</span>
          <span className="newflag">LIVE</span>
        </div>
        <div className="title">
          <span className="shimmer">{a.verb}</span>
          {" — "}
          <span ref={twRef} />
          <span className="cursor-char" />
        </div>
        <div className="thinkbar">
          <i />
        </div>
      </div>
      <div className="right">
        <div
          className="ring"
          style={
            {
              "--p": 30,
              "--c": "var(--agent-500)",
            } as React.CSSProperties
          }
        >
          <span>···</span>
        </div>
        <span className="status think">thinking</span>
      </div>
    </div>
  );
}

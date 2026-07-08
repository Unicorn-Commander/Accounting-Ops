"use client";

import { useEffect, useRef, useState } from "react";

type CashRibbonProps = {
  series: number[];
  min: number;
  max: number;
  current: number;
  delta: number;
  /** True when the series was reconstructed from real dated transactions. */
  hasHistory: boolean;
  /** Real number of connected accounts. */
  accountCount: number;
  /** Real cash-on-hand (assets), or null when no bank is connected. */
  availableNow: number | null;
  annotation?: {
    index: number;
    label: string;
    value: string;
  };
  /** Called when the big number is clicked — opens lineage popover */
  onBigNumClick?: () => void;
};

function fmt(n: number): string {
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function smoothPath(pts: [number, number][]): string {
  let d = `M ${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i - 1] || pts[i];
    const b = pts[i];
    const c = pts[i + 1];
    const e = pts[i + 2] || c;
    d += ` C ${b[0] + (c[0] - a[0]) / 6},${b[1] + (c[1] - a[1]) / 6} ${c[0] - (e[0] - b[0]) / 6},${c[1] - (e[1] - b[1]) / 6} ${c[0]},${c[1]}`;
  }
  return d;
}

export function CashRibbon({
  series: data,
  min,
  max,
  current,
  delta,
  hasHistory,
  accountCount,
  availableNow,
  annotation,
  onBigNumClick,
}: CashRibbonProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  // Re-render SVG on resize and theme change
  const [, setTick] = useState(0);
  useEffect(() => {
    function handler() {
      setTick((t) => t + 1);
    }
    window.addEventListener("resize", handler);
    return () => window.removeEventListener("resize", handler);
  }, []);

  const W = containerRef.current?.clientWidth ?? 860;
  const H = 184;
  const pad = 6;

  const range = max - min || 1;
  const X = (i: number) => pad + (i / (data.length - 1)) * (W - pad * 2);
  const Y = (v: number) =>
    H - pad - ((v - min) / range) * (H - pad * 2 - 12);

  const pts: [number, number][] = data.map((v, i) => [X(i), Y(v)]);
  const line = smoothPath(pts);
  const area = line + ` L ${X(data.length - 1)},${H} L ${X(0)},${H} Z`;

  // Gridlines
  let grid = "";
  for (let g = 1; g < 4; g++) {
    const y = pad + g * ((H - pad * 2) / 4);
    grid += `<line x1="0" y1="${y}" x2="${W}" y2="${y}" stroke="var(--hairline)" stroke-width="1"/>`;
  }

  // Annotation position
  const annotX = annotation ? X(annotation.index) : 0;
  const annotY = annotation ? Y(data[annotation.index]) : 0;

  const endX = X(data.length - 1);
  const endY = Y(data[data.length - 1]);

  const deltaUp = delta >= 0;
  const deltaStr = `${deltaUp ? "+" : ""}${delta.toFixed(1)}%`;

  const prefersReduced =
    typeof window !== "undefined"
      ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
      : false;

  const dashLen = W * 3; // approximates path length

  return (
    <div className="card-depth lift ribbonwrap">
      <div className="px-[18px] pt-[18px] pb-1">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="eyebrow">Net cash · last 90 days</div>
            <div
              className="bignum num inline-flex items-center gap-2 cursor-pointer transition hover:text-[var(--brand-200)]"
              onClick={onBigNumClick}
            >
              {fmt(current)}
              <span className="text-[11px] text-[var(--ink-4)] border border-[var(--hairline)] rounded-md px-1.5 py-0.5 font-medium tracking-normal hover:text-[var(--brand-300)] hover:border-[var(--brand-500)]">
                trace ⌥
              </span>
            </div>
            <div className="text-[12px] text-[var(--ink-3)] mt-1.5">
              {accountCount > 0
                ? `Across ${accountCount} connected ${accountCount === 1 ? "account" : "accounts"}`
                : "No accounts connected yet"}
              {availableNow != null && (
                <>
                  {" · "}
                  <span style={{ color: "var(--credit)" }}>{fmt(availableNow)} available now</span>
                </>
              )}
            </div>
          </div>
          <div className="text-right">
            {hasHistory && (
              <span
                className={`inline-flex items-center gap-1 text-[12px] font-semibold px-2 py-1 rounded-full ${
                  deltaUp ? "bg-credit/15 text-credit" : "bg-debit/15 text-debit"
                }`}
              >
                {deltaUp ? "▲" : "▼"} {deltaStr}
              </span>
            )}
            <div className="text-[11px] text-[var(--ink-4)] mt-2.5">last 90 days</div>
          </div>
        </div>
      </div>

      <div className="relative mt-2 px-[18px]" ref={containerRef}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          className="block w-full"
          style={{ height: H }}
        >
          <defs>
            <linearGradient id="ribbon-fill" x1="0" y1="0" x2="0" y2="1">
              <stop
                offset="0%"
                stopColor="var(--brand-500)"
                stopOpacity="0.45"
              />
              <stop
                offset="55%"
                stopColor="var(--brand-500)"
                stopOpacity="0.1"
              />
              <stop offset="100%" stopColor="var(--agent-500)" stopOpacity="0" />
            </linearGradient>
            <linearGradient id="ribbon-stroke" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stopColor="var(--brand-500)" />
              <stop offset="100%" stopColor="var(--agent-500)" />
            </linearGradient>
          </defs>
          {/* Gridlines */}
          <g dangerouslySetInnerHTML={{ __html: grid }} />
          {/* Area fill */}
          <path d={area} fill="url(#ribbon-fill)" />
          {/* Line with draw-in animation */}
          <path
            d={line}
            fill="none"
            stroke="url(#ribbon-stroke)"
            strokeWidth="2.5"
            strokeLinecap="round"
            style={{
              filter: "drop-shadow(0 5px 12px color-mix(in srgb, var(--brand-500) 40%, transparent))",
              ...(prefersReduced
                ? {}
                : {
                    strokeDasharray: String(dashLen),
                    strokeDashoffset: String(dashLen),
                  }),
            }}
          >
            {!prefersReduced && (
              <animate
                attributeName="stroke-dashoffset"
                from={String(dashLen)}
                to="0"
                dur="1.4s"
                fill="freeze"
                calcMode="spline"
                keySplines="0.2 0.7 0.2 1"
                keyTimes="0;1"
                values={`${dashLen};0`}
              />
            )}
          </path>
          {/* Pulsing endpoint */}
          <circle
            r="4.5"
            fill="var(--agent-500)"
            stroke="var(--surface)"
            strokeWidth="2"
            cx={endX}
            cy={endY}
          >
            {!prefersReduced && (
              <animate
                attributeName="r"
                values="4.5;7.5;4.5"
                dur="2s"
                repeatCount="indefinite"
              />
            )}
          </circle>
        </svg>

        {/* Annotation marker */}
        {annotation && (
          <div
            className="annot"
            style={{
              left: `${(annotX / W) * 100}%`,
              top: `${(annotY / H) * 100}%`,
            }}
          >
            <div className="t">{annotation.label}</div>
            <b>{annotation.value}</b>
          </div>
        )}
      </div>
    </div>
  );
}

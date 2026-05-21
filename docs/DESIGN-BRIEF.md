# Accounting-Ops Design Brief

Synthesized from research swarms 2026-05-21. Source of truth for UI implementation.

## Color tokens (HSL, dark-mode-first)

```css
/* Canvas — Mercury-influenced, slightly warmer */
--bg-canvas:      240 14% 7%;   /* #100F15 */
--bg-surface:     240 11% 12%;  /* #1B1A22 */
--bg-elevated:    240 10% 18%;  /* #2A2932 */
--bg-overlay:     240 8% 24%;   /* #3A3942 */

/* Borders */
--border-subtle:  255 255 255 / 0.06;
--border-default: 255 255 255 / 0.10;
--border-strong:  255 255 255 / 0.16;

/* Ink */
--ink-primary:    240 18% 94%;  /* #EDEDF3 */
--ink-secondary:  240 8% 68%;
--ink-tertiary:   240 9% 50%;
--ink-disabled:   240 8% 38%;

/* Brand — Unicorn Violet */
--brand-500:      265 78% 62%;  /* #8B5CF6 */
--brand-400:      265 85% 72%;
--brand-600:      265 70% 52%;
--brand-glow:     265 78% 62% / 0.35;

/* Agent accent — Unicorn cyan */
--agent-500:      188 95% 56%;  /* #22D3EE */
--agent-glow:     188 95% 56% / 0.30;

/* Financial semantic */
--credit:         157 64% 52%;  /* #34D399 */
--debit:          0 91% 71%;    /* #F87171 */
--pending:        43 96% 56%;   /* #FBBF24 */
--reconciled:     158 64% 52%;
--flagged:        0 91% 71%;

/* Confidence (Klarity-style heatmap) */
--conf-high:      157 64% 52%;  /* >0.90 */
--conf-medium:    43 96% 56%;   /* 0.70-0.90 */
--conf-low:       0 91% 71%;    /* <0.70 — always human-review */
```

## Typography

- **Display + UI:** Geist (700/600/500/420/400) — variable, free, Arcadia substitute
- **Numeric:** Geist Mono — tabular-figures by default for money/IDs/dates
- **Long-form (agent reasoning):** Inter 400/500 — signals "this is the agent talking, not chrome"

## Three distinguishing patterns

1. **Confidence is a first-class visual primitive.** Every agent-touched field gets a 6px confidence dot. Below 0.70 always routes to human queue. Klarity pattern.
2. **Agent Activity Ticker is always-on.** Bottom-right persistent monospace event stream, pulse-dot when active. Brex MessageStream pattern. "Your machine is working for you and you can audit it any time."
3. **Side-panel-over-modal, everywhere.** Modals interrupt; side panels preserve context. Stripe Dashboard pattern. Combined with Cmd+K command palette (Linear pattern).

## Page layouts

- **Dashboard:** 12-col grid. KPI cards top row (Cash, Burn, Runway, AR). Middle: cash chart + agent activity feed. Bottom: recent reconciliations + close-checklist progress.
- **Transactions:** Sticky filter bar. Virtualized table 70%. Right side-panel 30% slides in on row click. No modals.
- **Reconciliation Queue:** Kanban 3-col: Needs Review / Agent Suggested / Approved. Draggable cards. Bulk-approve when n>1.
- **Chat-with-Agent:** Split 60/40. Left = thread. Right = live workspace showing what agent touches.
- **Transparency Panel:** Right-sheet, 480-560px. Four tabs: Data | Logic | Reasoning | History.

## Stack

- Next.js 15 App Router
- Tailwind v4 with @theme block
- Radix primitives: Dialog (as Sheet), DropdownMenu, Tabs, Tooltip, Toast, Command (palette), Popover
- next/font/google for Geist, Geist Mono, Inter
- 150ms ease-out hovers; 220ms ease-out sheet slide-in; agent pulse 1.6s

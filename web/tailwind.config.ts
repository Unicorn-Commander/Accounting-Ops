import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Neutrals reference per-theme CSS channels so every utility
        // (bg-canvas, text-ink-primary, …) switches with the active theme and
        // still supports /opacity (e.g. bg-canvas/8). Channels live in globals.css.
        canvas: "hsl(var(--canvas-ch) / <alpha-value>)",
        "canvas-2": "hsl(var(--canvas-2-ch) / <alpha-value>)",
        surface: "hsl(var(--surface-ch) / <alpha-value>)",
        "surface-2": "hsl(var(--surface-2-ch) / <alpha-value>)",
        elevated: "hsl(var(--elevated-ch) / <alpha-value>)",
        overlay: "hsl(var(--overlay-ch) / <alpha-value>)",
        // Ink
        "ink-primary": "hsl(var(--ink-primary-ch) / <alpha-value>)",
        "ink-secondary": "hsl(var(--ink-secondary-ch) / <alpha-value>)",
        "ink-tertiary": "hsl(var(--ink-tertiary-ch) / <alpha-value>)",
        "ink-disabled": "hsl(var(--ink-disabled-ch) / <alpha-value>)",
        // Brand — Unicorn Violet
        brand: {
          200: "hsl(265 92% 86%)",
          300: "hsl(265 88% 79%)",
          400: "hsl(265 85% 72%)",
          500: "hsl(265 78% 62%)",
          600: "hsl(265 70% 52%)",
          700: "hsl(265 64% 43%)"
        },
        // Agent accent — Unicorn cyan
        agent: {
          300: "hsl(188 95% 76%)",
          400: "hsl(188 95% 66%)",
          500: "hsl(188 95% 56%)",
          600: "hsl(188 95% 46%)"
        },
        // Financial semantic
        credit: "hsl(157 64% 52%)",
        debit: "hsl(0 91% 71%)",
        pending: "hsl(43 96% 56%)",
        reconciled: "hsl(158 64% 52%)",
        flagged: "hsl(0 91% 71%)",
        blocked: "hsl(18 91% 60%)",
        // Confidence
        "conf-high": "hsl(157 64% 52%)",
        "conf-medium": "hsl(43 96% 56%)",
        "conf-low": "hsl(0 91% 71%)"
      },
      borderColor: {
        // Tinted by the primary-ink channel → light hairlines on dark,
        // dark hairlines on light. Switches with the theme.
        DEFAULT: "hsl(var(--ink-primary-ch) / 0.10)",
        subtle: "hsl(var(--ink-primary-ch) / 0.06)",
        strong: "hsl(var(--ink-primary-ch) / 0.16)"
      },
      fontFamily: {
        sans: ["var(--font-geist-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-geist-mono)", "ui-monospace", "monospace"],
        reasoning: ["var(--font-inter)", "system-ui", "sans-serif"]
      },
      fontSize: {
        "numeric-lg": ["1.75rem", { lineHeight: "1", fontWeight: "500" }],
        "numeric-xl": ["2.5rem", { lineHeight: "1", fontWeight: "500" }]
      },
      boxShadow: {
        card: "0 1px 0 0 hsla(0 0% 100% / 0.08) inset, 0 2px 5px hsla(0 0% 0% / 0.4), 0 14px 38px hsla(0 0% 0% / 0.46)",
        hover: "0 1px 0 0 hsla(0 0% 100% / 0.08) inset, 0 6px 14px hsla(0 0% 0% / 0.5), 0 26px 60px hsla(0 0% 0% / 0.55)",
        pop: "0 1px 0 0 hsla(0 0% 100% / 0.08) inset, 0 12px 34px hsla(0 0% 0% / 0.55), 0 36px 90px hsla(0 0% 0% / 0.6)",
        ticker: "0 8px 32px rgba(0,0,0,0.5)",
        "brand-glow": "0 0 0 1px hsla(265 80% 65% / 0.35), 0 0 30px hsla(265 80% 65% / 0.3)",
        "agent-glow": "0 0 0 1px hsla(188 95% 57% / 0.5), 0 0 24px hsla(188 95% 57% / 0.42)"
      },
      borderRadius: {
        "2xl": "17px"
      },
      animation: {
        "pulse-soft": "pulse-soft 1.6s ease-in-out infinite",
        "fade-in": "fade-in 220ms ease-out",
        "slide-in-left": "slide-in-left 220ms ease-out",
        "slide-in-right": "slide-in-right 220ms ease-out",
        "pulse-live": "pulse-live 2.4s infinite",
        "pulse-agent": "pulse-agent 2.2s infinite",
        "scan": "scan 1.5s infinite",
        "shimmer": "shimmer 2s linear infinite",
        "blink": "blink 1s steps(2) infinite",
        "toast-in": "toast-in 0.45s cubic-bezier(0.2, 0.8, 0.2, 1)",
        "overlay-in": "overlay-in 0.18s ease",
        "pop-in": "pop-in 0.22s cubic-bezier(0.2, 0.8, 0.2, 1)",
        "slide-in-down": "slide-in-down 0.5s cubic-bezier(0.2, 0.7, 0.2, 1)",
        "count-up": "count-up 0.9s cubic-bezier(0.2, 0.7, 0.2, 1)"
      },
      keyframes: {
        "pulse-soft": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.5" }
        },
        "fade-in": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" }
        },
        "slide-in-left": {
          from: { transform: "translateX(-100%)" },
          to: { transform: "translateX(0)" }
        },
        "slide-in-right": {
          from: { transform: "translateX(100%)" },
          to: { transform: "translateX(0)" }
        },
        "pulse-live": {
          "0%": { boxShadow: "0 0 0 0 hsla(157 66% 53% / 0.55)" },
          "70%": { boxShadow: "0 0 0 7px hsla(157 66% 53% / 0)" },
          "100%": { boxShadow: "0 0 0 0 hsla(157 66% 53% / 0)" }
        },
        "pulse-agent": {
          "0%": { boxShadow: "0 0 0 0 hsla(188 95% 57% / 0.6)" },
          "70%": { boxShadow: "0 0 0 6px hsla(188 95% 57% / 0)" },
          "100%": { boxShadow: "0 0 0 0 hsla(188 95% 57% / 0)" }
        },
        scan: {
          from: { transform: "translateX(-120%)" },
          to: { transform: "translateX(360%)" }
        },
        shimmer: {
          to: { backgroundPosition: "-200% 0" }
        },
        blink: {
          "50%": { opacity: "0" }
        },
        "toast-in": {
          from: { opacity: "0", transform: "translateX(22px) scale(0.97)" },
          to: { opacity: "1", transform: "none" }
        },
        "overlay-in": {
          from: { opacity: "0" },
          to: { opacity: "1" }
        },
        "pop-in": {
          from: { opacity: "0", transform: "translateY(-14px) scale(0.98)" },
          to: { opacity: "1", transform: "none" }
        },
        "slide-in-down": {
          from: { opacity: "0", transform: "translateY(-12px) scale(0.99)" },
          to: { opacity: "1", transform: "none" }
        },
        "count-up": {
          from: { opacity: "0" },
          to: { opacity: "1" }
        }
      }
    }
  },
  plugins: []
} satisfies Config;

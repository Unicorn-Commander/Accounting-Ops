import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Canvas
        canvas: "hsl(240 14% 7%)",
        surface: "hsl(240 11% 12%)",
        elevated: "hsl(240 10% 18%)",
        overlay: "hsl(240 8% 24%)",
        // Ink
        "ink-primary": "hsl(240 18% 94%)",
        "ink-secondary": "hsl(240 8% 68%)",
        "ink-tertiary": "hsl(240 9% 50%)",
        "ink-disabled": "hsl(240 8% 38%)",
        // Brand — Unicorn Violet
        brand: {
          400: "hsl(265 85% 72%)",
          500: "hsl(265 78% 62%)",
          600: "hsl(265 70% 52%)"
        },
        // Agent accent — Unicorn cyan
        agent: {
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
        DEFAULT: "rgba(255,255,255,0.10)",
        subtle: "rgba(255,255,255,0.06)",
        strong: "rgba(255,255,255,0.16)"
      },
      fontFamily: {
        sans: ["var(--font-geist-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-geist-mono)", "ui-monospace", "monospace"],
        reasoning: ["var(--font-inter)", "system-ui", "sans-serif"]
      },
      fontSize: {
        "numeric-lg": ["1.75rem", { lineHeight: "1", letterSpacing: "-0.025em", fontWeight: "500" }],
        "numeric-xl": ["2.5rem", { lineHeight: "1", letterSpacing: "-0.025em", fontWeight: "500" }]
      },
      boxShadow: {
        card: "0 4px 24px rgba(0,0,0,0.4)",
        ticker: "0 8px 32px rgba(0,0,0,0.5)",
        "brand-glow": "0 0 24px hsl(265 78% 62% / 0.35)",
        "agent-glow": "0 0 8px hsl(188 95% 56% / 0.6)"
      },
      animation: {
        "pulse-soft": "pulse-soft 1.6s ease-in-out infinite",
        "fade-in": "fade-in 220ms ease-out",
        "slide-in-left": "slide-in-left 220ms ease-out"
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
        }
      }
    }
  },
  plugins: []
} satisfies Config;

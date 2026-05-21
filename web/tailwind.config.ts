import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#152018",
        field: "#f6f8f3",
        line: "#d9dfd2",
        signal: "#1f7a5a",
        warn: "#b25b2a",
        paper: "#ffffff"
      }
    }
  },
  plugins: []
} satisfies Config;

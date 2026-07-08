// Minimal ESLint flat config for Next.js 15.
// We use `next lint` (deprecated but still functional) — this config keeps it
// non-interactive without pulling in the full Next plugin (which has a
// circular-JSON issue with the current legacy compat shim).

export default [
  {
    ignores: ["node_modules", ".next", "out", "dist", "tests"]
  },
  {
    rules: {}
  }
];

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // Legal draft markdown is read at request time via fs; ensure standalone
  // tracing packs content/legal into the production image.
  outputFileTracingIncludes: {
    "/terms": ["./content/legal/**/*"],
    "/privacy": ["./content/legal/**/*"],
    "/legal": ["./content/legal/**/*"],
  },
};

export default nextConfig;

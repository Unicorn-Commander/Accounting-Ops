// Avatar/colour config for stream agents. Named entries give the demo agents
// their hand-tuned look; resolveAgent() resolves ANY agent id (including real
// backend agent_ids like "anthropic/cpa-tax-resolver") to a stable config —
// known ones by substring, unknown ones via a deterministic hue hash so the
// same agent always renders the same colour.

export type AgentConfig = {
  c1: string;
  c2: string;
  h: string;
  ini: string;
  verb: string;
};

export const AGENT_CONFIG: Record<string, AgentConfig> = {
  "recon-1.2": { c1: "hsl(188 80% 50%)", c2: "hsl(200 80% 45%)", h: "188 85% 55%", ini: "R", verb: "Reconciling" },
  compliance: { c1: "hsl(0 70% 60%)", c2: "hsl(12 75% 52%)", h: "0 80% 64%", ini: "C", verb: "Checking" },
  bookkeeper: { c1: "hsl(265 70% 62%)", c2: "hsl(280 65% 50%)", h: "265 80% 66%", ini: "B", verb: "Reading" },
  "tax-rules": { c1: "hsl(43 90% 55%)", c2: "hsl(30 85% 50%)", h: "38 92% 56%", ini: "T", verb: "Computing" },
  "ap-pay": { c1: "hsl(157 55% 48%)", c2: "hsl(170 55% 42%)", h: "157 60% 50%", ini: "A", verb: "Verifying" },
};

// Substring → named config, so real agent ids that contain a known role still
// pick up the nice palette (e.g. ".../reconciler" → recon look).
const ALIASES: Array<[RegExp, string]> = [
  [/recon/i, "recon-1.2"],
  [/(compliance|sentinel|rule)/i, "compliance"],
  [/(book|categor|ledger)/i, "bookkeeper"],
  [/tax/i, "tax-rules"],
  [/(ap-?pay|payment|payable|ach)/i, "ap-pay"],
];

function hashHue(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h % 360;
}

export function resolveAgent(agentId: string): AgentConfig {
  if (!agentId) return { c1: "hsl(240 8% 50%)", c2: "hsl(240 8% 40%)", h: "240 8% 55%", ini: "•", verb: "Working" };
  if (AGENT_CONFIG[agentId]) return AGENT_CONFIG[agentId];
  for (const [re, key] of ALIASES) {
    if (re.test(agentId)) return AGENT_CONFIG[key];
  }
  // Deterministic fallback for any unknown real agent id.
  const seg = agentId.includes("/") ? agentId.split("/").pop()! : agentId;
  const hue = hashHue(agentId);
  const ini = (seg.replace(/[^a-z0-9]/gi, "")[0] ?? "A").toUpperCase();
  return {
    c1: `hsl(${hue} 70% 58%)`,
    c2: `hsl(${(hue + 26) % 360} 64% 47%)`,
    h: `${hue} 75% 60%`,
    ini,
    verb: "Working",
  };
}

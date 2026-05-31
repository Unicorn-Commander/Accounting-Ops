// Persona shape exposed by GET /api/chat/personas — keep 1:1 with what
// the Python registry returns from list_personas(brigade_online=...).
//
// The Python side uses slug (kebab-case stable id) and one_line; the web
// matches that vocabulary.

export type PersonaTier = "strategic" | "specialist" | "operational";

export interface Persona {
  slug: string;
  display_name: string;
  one_line: string;
  tier: PersonaTier;
  needs_tax_year: boolean;
  consumes_brigade: boolean;
  brigade_active: boolean;
  default_model: string | null;
  tool_count: number;
  cross_app_tool_count: number;
}

export interface PersonasResponse {
  default: string;
  brigade_online: boolean;
  brigade_reason: string;
  personas: Persona[];
}

/** Hard-coded fallback when /api/chat/personas isn't reachable (e.g. during
 *  the next-dev build or on a fresh worktree with no app container).
 *
 *  Mirrors the registry in agents/chat/persona_registry.py so the UI is
 *  always demoable. The live API result wins when it's present. */
export const FALLBACK_PERSONAS: Persona[] = [
  {
    slug: "cfo-agent",
    display_name: "CFO Agent",
    one_line:
      "Strategic financial view — cash runway, unit economics, board reporting, financing strategy.",
    tier: "strategic",
    needs_tax_year: true,
    consumes_brigade: true,
    brigade_active: false,
    default_model: "claude-opus-4-7",
    tool_count: 22,
    cross_app_tool_count: 0
  },
  {
    slug: "cpa-tax-resolver",
    display_name: "CPA Tax Resolver",
    one_line: "Resolves queue items + cites rules; default conversational entry point.",
    tier: "strategic",
    needs_tax_year: true,
    consumes_brigade: false,
    brigade_active: false,
    default_model: null,
    tool_count: 12,
    cross_app_tool_count: 0
  },
  {
    slug: "setup-assistant",
    display_name: "Setup Assistant",
    one_line:
      "Conversationally provisions a new workspace, entity, COA, and connectors. Run this first.",
    tier: "operational",
    needs_tax_year: false,
    consumes_brigade: false,
    brigade_active: false,
    default_model: null,
    tool_count: 10,
    cross_app_tool_count: 0
  },
  {
    slug: "form-1120-preparer",
    display_name: "Form 1120 Preparer",
    one_line: "Drafts federal C-corp returns line by line.",
    tier: "specialist",
    needs_tax_year: true,
    consumes_brigade: false,
    brigade_active: false,
    default_model: null,
    tool_count: 6,
    cross_app_tool_count: 0
  },
  {
    slug: "sc-state-specialist",
    display_name: "SC State Specialist",
    one_line: "South Carolina SC1120 + state-specific compliance.",
    tier: "specialist",
    needs_tax_year: true,
    consumes_brigade: false,
    brigade_active: false,
    default_model: null,
    tool_count: 12,
    cross_app_tool_count: 0
  },
  {
    slug: "state-strategy-drafter",
    display_name: "State Strategy Drafter",
    one_line: "Multi-state nexus + apportionment strategy memos.",
    tier: "specialist",
    needs_tax_year: true,
    consumes_brigade: false,
    brigade_active: false,
    default_model: null,
    tool_count: 12,
    cross_app_tool_count: 0
  },
  {
    slug: "federal-strategy-drafter",
    display_name: "Federal Strategy Drafter",
    one_line: "Year-end federal tax-planning analysis (R&D, NOL, §199A, etc.).",
    tier: "specialist",
    needs_tax_year: true,
    consumes_brigade: false,
    brigade_active: false,
    default_model: null,
    tool_count: 5,
    cross_app_tool_count: 0
  },
  {
    slug: "1099-specialist",
    display_name: "1099-NEC Specialist",
    one_line: "Identifies 1099 payees + drafts year-end information returns.",
    tier: "specialist",
    needs_tax_year: true,
    consumes_brigade: false,
    brigade_active: false,
    default_model: null,
    tool_count: 5,
    cross_app_tool_count: 0
  },
  {
    slug: "officer-compensation-specialist",
    display_name: "Officer Compensation Specialist",
    one_line: "Form 1125-E + reasonable-comp analysis for C-corp officers.",
    tier: "specialist",
    needs_tax_year: true,
    consumes_brigade: false,
    brigade_active: false,
    default_model: null,
    tool_count: 12,
    cross_app_tool_count: 0
  },
  {
    slug: "nol-schedule-l-analyst",
    display_name: "NOL / Schedule L Analyst",
    one_line: "Net operating loss carryforwards + balance-sheet reconciliation.",
    tier: "specialist",
    needs_tax_year: true,
    consumes_brigade: false,
    brigade_active: false,
    default_model: null,
    tool_count: 12,
    cross_app_tool_count: 0
  },
  {
    slug: "audit-defense-specialist",
    display_name: "Audit Defense Specialist",
    one_line: "Builds defensible positions + audit-readiness packages.",
    tier: "specialist",
    needs_tax_year: true,
    consumes_brigade: false,
    brigade_active: false,
    default_model: null,
    tool_count: 12,
    cross_app_tool_count: 0
  },
  {
    slug: "personal-1040-specialist",
    display_name: "Personal 1040 Specialist",
    one_line:
      "Individual returns with military retirement / VA disability / SC veteran provisions.",
    tier: "specialist",
    needs_tax_year: true,
    consumes_brigade: false,
    brigade_active: false,
    default_model: null,
    tool_count: 5,
    cross_app_tool_count: 0
  }
];

export const TIER_LABELS: Record<PersonaTier, string> = {
  strategic: "Strategic",
  specialist: "Specialist",
  operational: "Operational"
};

export const TIER_DESCRIPTIONS: Record<PersonaTier, string> = {
  strategic: "High-level synthesis — board-room framing, runway, year-end strategy.",
  specialist: "Deep expertise on one form or area — line-by-line work.",
  operational: "Workflow + setup — onboarding, anomaly detection, runtime ops."
};

export const TIER_ORDER: PersonaTier[] = ["strategic", "specialist", "operational"];

export function groupByTier(personas: Persona[]): Record<PersonaTier, Persona[]> {
  const grouped: Record<PersonaTier, Persona[]> = {
    strategic: [],
    specialist: [],
    operational: []
  };
  for (const p of personas) {
    if (p.tier in grouped) grouped[p.tier].push(p);
  }
  return grouped;
}

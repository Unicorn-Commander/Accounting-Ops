"""dispatch_plan.py — Deterministic dispatch planner for the Tax Operations Coordinator.

Parses user intent + entity context into a structured dispatch plan (ordered list of
specialists to invoke). No LLM is used at this layer — classification is rule-based
for predictability and auditability.

Usage (as a module)::

    from agents.tax_specialists.coordinator.dispatch_plan import build_dispatch_plan

    plan = build_dispatch_plan(
        user_request="prepare 2023 returns for Magic Unicorn Inc",
        entity_id="aabbccdd-1234-5678-9abc-000000000001",
        tax_year=2023,
        workspace_id="550e8400-e29b-41d4-a716-446655440000",
    )
    for step in plan.steps:
        print(step.specialist_slug, step.parallel_group, step.required)

Usage (CLI — prints the plan as JSON)::

    python agents/tax_specialists/coordinator/dispatch_plan.py \\
        --request "prepare 2023 returns" \\
        --entity-id aabbccdd-1234-5678-9abc-000000000001 \\
        --tax-year 2023
"""

from __future__ import annotations

import argparse
import json
import re
import textwrap
from dataclasses import asdict, dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Specialist registry
#
# Each entry describes a known specialist. The Coordinator only dispatches
# specialists that appear in this registry. Adding a new specialist requires:
#   1. Adding an entry here.
#   2. Ensuring the specialist's run_specialist.py exists at the expected path.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpecialistMeta:
    """Static metadata for a registered specialist."""

    slug: str
    """Short identifier used in file names and subprocess paths."""

    display_name: str
    """Human-readable name for reports and memos."""

    runner_path: str
    """Path to run_specialist.py relative to the repo root."""

    parallel_group: int
    """Specialists with the same group number may run in parallel.
    Groups are executed in ascending order. Lower group = earlier.
    Group 1 runs first; group 2 waits for all group-1 specialists to finish, etc.
    """

    available: bool = True
    """False if this specialist is planned but not yet built.
    The Coordinator writes a gap notice for unavailable specialists instead of
    dispatching them.
    """


# Ordered by their default group number (1 = first wave, 2 = second wave).
SPECIALIST_REGISTRY: list[SpecialistMeta] = [
    SpecialistMeta(
        slug="form_1120_preparer",
        display_name="1120 Preparer",
        runner_path="agents/tax_specialists/form_1120_preparer/run_specialist.py",
        parallel_group=1,
        available=True,
    ),
    SpecialistMeta(
        slug="1099_specialist",
        display_name="1099 Specialist",
        runner_path="agents/tax_specialists/1099_specialist/run_specialist.py",
        parallel_group=1,
        available=True,  # built 2026-05-29
    ),
    SpecialistMeta(
        slug="sc_state_specialist",
        display_name="SC State Specialist",
        runner_path="agents/tax_specialists/sc_state_specialist/run_specialist.py",
        parallel_group=2,  # Must wait for group-1 (1120 Preparer) to finish
        available=True,  # built 2026-05-29
    ),
    SpecialistMeta(
        slug="audit_defense_specialist",
        display_name="Audit Defense Specialist",
        runner_path="agents/tax_specialists/audit_defense_specialist/run_specialist.py",
        parallel_group=3,  # Runs after SC State (group 2)
        available=False,  # sub-agent still in flight — flip when landed
    ),
    SpecialistMeta(
        slug="officer_compensation_specialist",
        display_name="Officer Compensation Specialist",
        runner_path="agents/tax_specialists/officer_compensation_specialist/run_specialist.py",
        parallel_group=1,
        available=True,  # built 2026-05-29
    ),
    SpecialistMeta(
        slug="nol_schedule_l_analyst",
        display_name="NOL + Schedule L Analyst",
        runner_path="agents/tax_specialists/nol_schedule_l_analyst/run_specialist.py",
        parallel_group=2,  # Needs federal data first
        available=True,  # built 2026-05-29
    ),
    SpecialistMeta(
        slug="payroll_advisor",
        display_name="Payroll Advisor",
        runner_path="agents/tax_specialists/payroll_advisor/run_specialist.py",
        parallel_group=1,
        available=False,  # Not yet built — produces gap notice
    ),
]

_REGISTRY_BY_SLUG: dict[str, SpecialistMeta] = {s.slug: s for s in SPECIALIST_REGISTRY}


# ---------------------------------------------------------------------------
# Dispatch step
# ---------------------------------------------------------------------------

@dataclass
class DispatchStep:
    """One specialist invocation in the dispatch plan."""

    specialist_slug: str
    display_name: str
    runner_path: str
    parallel_group: int
    available: bool

    # Resolved at plan-build time from the user request + context.
    entity_id: str = ""
    workspace_id: str = ""
    tax_year: int = 0

    # Set to True if this specialist is required for the run to be meaningful.
    # If a required specialist is unavailable, the plan is marked incomplete.
    required: bool = True

    # Gap notice text — set when available=False.
    gap_notice: str = ""


# ---------------------------------------------------------------------------
# Dispatch plan
# ---------------------------------------------------------------------------

@dataclass
class DispatchPlan:
    """Full dispatch plan for one coordinator run."""

    user_request: str
    entity_id: str
    workspace_id: str
    tax_year: int
    intent: str  # Matched intent key, e.g. "full_trio" or "federal_only"
    steps: list[DispatchStep] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    is_complete: bool = True  # False if a required specialist is unavailable

    def parallel_groups(self) -> dict[int, list[DispatchStep]]:
        """Return steps grouped by parallel_group (ascending order).

        Steps within a group may be launched concurrently.
        Groups must be processed in ascending key order — each group waits for
        all steps in the prior group to finish before starting.
        """
        groups: dict[int, list[DispatchStep]] = {}
        for step in self.steps:
            groups.setdefault(step.parallel_group, []).append(step)
        return dict(sorted(groups.items()))

    def available_steps(self) -> list[DispatchStep]:
        """Return only the steps whose specialist is available (built)."""
        return [s for s in self.steps if s.available]

    def gap_steps(self) -> list[DispatchStep]:
        """Return only the steps whose specialist is NOT yet available."""
        return [s for s in self.steps if not s.available]

    def to_dict(self) -> dict:
        """Serialize to a JSON-serializable dict (for --dry-run output)."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Intent classifier
#
# Maps (normalized) user request text to a named intent.
# Rule-based; no LLM. Patterns are tried in order; first match wins.
# ---------------------------------------------------------------------------

# Intent → list of specialist slugs to dispatch (in the order they should be added).
# The parallel_group on the SpecialistMeta governs actual execution order;
# this list just enumerates which specialists are selected for the intent.
_INTENT_MAP: dict[str, list[str]] = {
    "full_trio": [
        "form_1120_preparer",
        "1099_specialist",
        "sc_state_specialist",
    ],
    "federal_only": [
        "form_1120_preparer",
    ],
    "information_returns_only": [
        "1099_specialist",
    ],
    "sc_state_only": [
        # SC State requires federal data — add 1120 Preparer as a dependency
        # even when the user only asked for SC State. The 1120 Preparer runs
        # at group 1 so SC State (group 2) gets its data.
        "form_1120_preparer",
        "sc_state_specialist",
    ],
    "audit_defense": [
        "sc_state_specialist",
        "audit_defense",
    ],
    "payroll": [
        "payroll_advisor",
    ],
    "unknown": [],  # Gap — nothing to dispatch
}

# Regex patterns → intent key. Tried in order; first match wins.
# Patterns are applied to the lowercased, whitespace-normalized request.
_INTENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Audit / SCDOR notice patterns — check before generic "SC" patterns.
    (re.compile(r"\b(audit\s+defense|respond\s+to|scdor\s+notice|irs\s+notice|cp\d{3})\b"), "audit_defense"),
    # Payroll
    (re.compile(r"\b(payroll|form\s+941|w-?2|wages)\b"), "payroll"),
    # Information returns only
    (re.compile(r"\b(1099|information\s+returns?|information\s+reporting)\b"), "information_returns_only"),
    # Federal only — explicit qualifiers
    (re.compile(r"\b(federal|form\s+1120|1120)\b"), "federal_only"),
    # SC / state only — explicit qualifiers (no "federal" in the same request)
    (re.compile(r"\b(sc|south\s+carolina|state\s+returns?|sc1120)\b"), "sc_state_only"),
    # Generic "prepare … returns" / "do … taxes" / "file … taxes" — full trio
    (re.compile(r"\b(prepare|draft|file|do|complete|run)\b.{0,60}\b(returns?|taxes?|tax\s+package)\b"), "full_trio"),
    # Fallback — unknown
    (re.compile(r".*"), "unknown"),
]


def _classify_intent(user_request: str) -> str:
    """Classify a user request string into a named intent.

    Returns one of the keys in _INTENT_MAP.
    Classification is deterministic — no randomness, no LLM.
    """
    normalized = re.sub(r"\s+", " ", user_request.strip().lower())

    for pattern, intent_key in _INTENT_PATTERNS:
        if pattern.search(normalized):
            return intent_key

    return "unknown"


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------

def build_dispatch_plan(
    user_request: str,
    entity_id: str,
    tax_year: int,
    workspace_id: str = "",
) -> DispatchPlan:
    """Build a deterministic dispatch plan from a user request + entity context.

    Args:
        user_request:  Natural-language request from the user. Case-insensitive.
        entity_id:     Entity UUID to operate on.
        tax_year:      Four-digit tax year.
        workspace_id:  Workspace UUID (optional — passed through to steps).

    Returns:
        A DispatchPlan listing which specialists to invoke, in what parallel groups,
        with gap notices for any unavailable specialists.

    Notes:
        - The plan does not invoke any subprocess. Invocation is the runner's job.
        - The plan does not call any MCP tool. Sanity checks are the runner's job.
        - If the intent is "unknown", the plan has no steps and is_complete=False.
    """
    intent = _classify_intent(user_request)
    slugs = _INTENT_MAP.get(intent, [])

    steps: list[DispatchStep] = []
    gaps: list[str] = []
    is_complete = True

    for slug in slugs:
        meta = _REGISTRY_BY_SLUG.get(slug)
        if meta is None:
            # Slug in INTENT_MAP but not in registry — configuration error.
            gap_msg = (
                f"Specialist slug '{slug}' appears in the intent map but is not "
                f"registered in SPECIALIST_REGISTRY. This is a configuration error — "
                f"add the specialist to SPECIALIST_REGISTRY or remove it from the intent map."
            )
            gaps.append(gap_msg)
            is_complete = False
            continue

        gap_notice = ""
        if not meta.available:
            gap_notice = (
                f"Specialist '{meta.display_name}' ({slug}) is listed in the dispatch "
                f"plan for intent '{intent}' but has not been built yet. "
                f"Expected runner path: {meta.runner_path}. "
                f"CPA must cover this specialist's scope manually until it is implemented."
            )
            gaps.append(gap_notice)
            # Do NOT mark is_complete=False for unavailable-but-planned specialists;
            # the run can still proceed with available specialists. The runner will
            # write gap notices for the missing ones.

        steps.append(
            DispatchStep(
                specialist_slug=slug,
                display_name=meta.display_name,
                runner_path=meta.runner_path,
                parallel_group=meta.parallel_group,
                available=meta.available,
                entity_id=entity_id,
                workspace_id=workspace_id,
                tax_year=tax_year,
                required=(intent != "unknown"),
                gap_notice=gap_notice,
            )
        )

    if intent == "unknown":
        gaps.append(
            f"Could not classify the user request into a known intent. "
            f"Request: '{user_request[:200]}'. "
            f"No specialists dispatched. CPA must review the request manually."
        )
        is_complete = False

    return DispatchPlan(
        user_request=user_request,
        entity_id=entity_id,
        workspace_id=workspace_id,
        tax_year=tax_year,
        intent=intent,
        steps=steps,
        gaps=gaps,
        is_complete=is_complete,
    )


# ---------------------------------------------------------------------------
# CLI entry point — prints the plan as JSON for inspection / dry-run
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print the dispatch plan for a given user request as JSON. "
            "Does NOT invoke any specialist or MCP tool."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python dispatch_plan.py --request "prepare 2023 returns" --entity-id abc --tax-year 2023
              python dispatch_plan.py --request "respond to SCDOR notice" --entity-id abc --tax-year 2023
              python dispatch_plan.py --request "prepare 2023 federal returns" --entity-id abc --tax-year 2023
        """),
    )
    parser.add_argument("--request", required=True, help="User request text")
    parser.add_argument("--entity-id", required=True, help="Entity UUID")
    parser.add_argument("--tax-year", required=True, type=int, help="Tax year")
    parser.add_argument("--workspace-id", default="", help="Workspace UUID")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    plan = build_dispatch_plan(
        user_request=args.request,
        entity_id=args.entity_id,
        tax_year=args.tax_year,
        workspace_id=args.workspace_id,
    )
    print(json.dumps(plan.to_dict(), indent=2))


if __name__ == "__main__":
    main()

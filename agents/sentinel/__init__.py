"""Sentinel — autonomous CPA daemon.

Continuously scans workspaces + the tax/regulatory landscape for what matters:
filing deadlines, approval queue drift, regulatory updates, tax credits, §174
compliance, etc. Drafts memos, flags items, sends invitations, schedules
reminders. Humans intervene only on judgment calls.

See ``agents/_shared/sentinel_runbook.md`` for operator instructions.
"""

from agents.sentinel.config import (
    AutonomyLevel,
    SentinelConfig,
    get_or_create_config,
)

__all__ = ["AutonomyLevel", "SentinelConfig", "get_or_create_config"]

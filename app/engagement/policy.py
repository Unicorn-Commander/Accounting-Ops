"""Per-workspace EngagementPolicy read/lazy-create + capability helpers.

Mirrors ``agents/sentinel/config.py``: a frozen dataclass snapshot, a
lazy ``get_or_create_policy`` (INSERT defaults ON CONFLICT DO NOTHING, tolerant
parse with a safe fallback), and small capability predicates the rest of the app
gates on. The SAFE DEFAULT is ``pro_required`` so existing workspaces are
unchanged until someone explicitly opts into self-attestation.

All functions run inside the caller's transaction; the workspace search_path +
``app.current_workspace`` RLS GUC must already be set (use ``workspace_session``).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ledger import ReviewMode

_log = logging.getLogger("acctops.engagement.policy")

# The submission-rail channels the dormant rail knows about. Every channel
# defaults OFF; PaperPrint is always available and is NOT a gated channel.
RAIL_CHANNELS: tuple[str, ...] = ("efile_irs", "state_portal", "eftps", "esign")

DEFAULT_REVIEW_MODE = ReviewMode.PRO_REQUIRED


@dataclass(frozen=True)
class EngagementPolicyData:
    workspace_id: UUID
    review_mode: ReviewMode
    has_licensed_pro: bool
    prefer_local_inference: bool
    submission_channels: dict = field(default_factory=dict)
    rail_kill_switch: bool = False

    # -- capability predicates ---------------------------------------------
    @property
    def can_self_attest(self) -> bool:
        """True if a user may self-certify a work product to completion (no pro)."""
        return self.review_mode == ReviewMode.SELF_ATTEST_ALLOWED

    @property
    def requires_pro_review(self) -> bool:
        """True if a licensed professional must review before leaving DRAFT."""
        return self.review_mode == ReviewMode.PRO_REQUIRED

    @property
    def autonomous_disabled(self) -> bool:
        return self.review_mode == ReviewMode.AUTONOMOUS_OFF

    def channel_enabled(self, channel: str) -> bool:
        """True only if a transmission channel is explicitly enabled AND the
        global kill-switch is off. Default-deny."""
        if self.rail_kill_switch:
            return False
        return bool(self.submission_channels.get(channel, False))


def _parse_review_mode(raw: str | None) -> ReviewMode:
    value = (raw or DEFAULT_REVIEW_MODE.value).strip().lower()
    try:
        return ReviewMode(value)
    except ValueError:
        _log.warning(
            "Unknown review_mode %r; falling back to %s",
            raw, DEFAULT_REVIEW_MODE.value,
        )
        return DEFAULT_REVIEW_MODE


def _coerce_channels(raw: object) -> dict:
    if isinstance(raw, dict):
        return {k: bool(v) for k, v in raw.items()}
    return {}


async def get_or_create_policy(
    session: AsyncSession, workspace_id: UUID
) -> EngagementPolicyData:
    """Fetch the engagement_policy row for the workspace; insert defaults if absent."""
    row = (
        await session.execute(
            text(
                """
                SELECT review_mode, has_licensed_pro, prefer_local_inference,
                       submission_channels, rail_kill_switch
                FROM engagement_policy
                WHERE workspace_id = :wid
                """
            ),
            {"wid": str(workspace_id)},
        )
    ).mappings().first()

    if row is None:
        await session.execute(
            text(
                """
                INSERT INTO engagement_policy (
                    workspace_id, review_mode, has_licensed_pro,
                    prefer_local_inference, submission_channels, rail_kill_switch
                ) VALUES (
                    :wid, 'pro_required', false, false, '{}'::jsonb, false
                )
                ON CONFLICT (workspace_id) DO NOTHING
                """
            ),
            {"wid": str(workspace_id)},
        )
        return EngagementPolicyData(
            workspace_id=workspace_id,
            review_mode=DEFAULT_REVIEW_MODE,
            has_licensed_pro=False,
            prefer_local_inference=False,
            submission_channels={},
            rail_kill_switch=False,
        )

    return EngagementPolicyData(
        workspace_id=workspace_id,
        review_mode=_parse_review_mode(row["review_mode"]),
        has_licensed_pro=bool(row["has_licensed_pro"]),
        prefer_local_inference=bool(row["prefer_local_inference"]),
        submission_channels=_coerce_channels(row["submission_channels"]),
        rail_kill_switch=bool(row["rail_kill_switch"]),
    )


async def get_prefer_local(workspace_id: UUID | str) -> bool:
    """True if the workspace prefers local-first (sovereign qwen) inference.

    Opens its own workspace_session (binds the RLS GUC + search_path) and reads
    the engagement_policy, lazily creating it with safe defaults. Used by the
    chat inference resolver, which passes a string workspace_id.
    """
    from app.db import SessionLocal
    from app.workspace_session import workspace_session

    wid = workspace_id if isinstance(workspace_id, UUID) else UUID(str(workspace_id))
    async with workspace_session(SessionLocal, wid) as session:
        policy = await get_or_create_policy(session, wid)
        return policy.prefer_local_inference


async def update_policy(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    review_mode: str | None = None,
    has_licensed_pro: bool | None = None,
    prefer_local_inference: bool | None = None,
    submission_channels: dict | None = None,
    rail_kill_switch: bool | None = None,
) -> None:
    """Patch the workspace's engagement_policy row (ensures it exists first)."""
    await get_or_create_policy(session, workspace_id)

    updates: list[str] = []
    params: dict = {"wid": str(workspace_id)}
    if review_mode is not None:
        updates.append("review_mode = :rm")
        params["rm"] = review_mode.strip().lower()
    if has_licensed_pro is not None:
        updates.append("has_licensed_pro = :hp")
        params["hp"] = has_licensed_pro
    if prefer_local_inference is not None:
        updates.append("prefer_local_inference = :pl")
        params["pl"] = prefer_local_inference
    if submission_channels is not None:
        updates.append("submission_channels = CAST(:sc AS jsonb)")
        params["sc"] = json.dumps({k: bool(v) for k, v in submission_channels.items()})
    if rail_kill_switch is not None:
        updates.append("rail_kill_switch = :ks")
        params["ks"] = rail_kill_switch

    if updates:
        updates.append("updated_at = now()")
        await session.execute(
            text(
                f"UPDATE engagement_policy SET {', '.join(updates)} "
                f"WHERE workspace_id = :wid"
            ),
            params,
        )

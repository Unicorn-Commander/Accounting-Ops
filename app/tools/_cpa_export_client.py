"""Shared client for the accounting-ops-cpa-export return endpoints.

Extracted so ``get_form_1120s_data`` and ``get_form_1065_data`` reuse ONE copy
of the auth handshake rather than each carrying a transcription of it — the
pattern established (and audited) in ``tax_get_form_1120_data``.

Audit 2026-05-31 Bug 10 — identity forwarding
=============================================
The sidecar must authorize the ACTUAL caller, not a static service token whose
permissions could drift broader than the caller's. So after the local
``authorize_workspace`` succeeds we mint a short-lived HMAC-signed internal
assertion bound to (uc_uid, workspace_id, entity_id, role, scopes) and send it
in ``X-Internal-Assertion``. The sidecar verifies it with the same master key
and runs its own membership + entity-scope checks against that identity.

``ACCTOPS_SERVICE_TOKEN`` is still attached as a bearer, but only as a transport
handshake so an unauthenticated caller cannot reach the sidecar at all.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from app.config import get_settings
from app.mcp_auth import authorize_workspace, current_auth
from app.security.crypto import get_crypto
from app.security.internal_assertion import sign_assertion

_log = logging.getLogger("acctops.cpa_export")

_CPA_EXPORT_ALLOW_BEARER_ONLY_ENV = "CPA_EXPORT_ALLOW_BEARER_ONLY"
_CPA_EXPORT_IDENTITY_ASSERTION_ERROR = "cpa-export identity assertion unavailable"
_CPA_EXPORT_CONSOLIDATED_ENTITY_IDS = frozenset({"all", "consolidated"})

_DRAFT_NOTICE = (
    "DRAFT — for CPA review only. "
    "This package was generated from the platform's journal entries and has NOT "
    "been reviewed or approved by a licensed tax professional. "
    "Do not file without CPA sign-off."
)

# Env var names that may carry the outbound cpa-export transport bearer, in
# precedence order. THE ONE PLACE this list lives — every cpa-export caller
# must resolve through resolve_cpa_export_bearer() so a deployment that sets a
# different-but-reasonable name cannot silently lose auth on one call site.
#
# 2026-08-11 incident: legacy1 (.ai prod) set ONLY CPA_EXPORT_SERVICE_TOKEN —
# the name the cpa-export SIDECAR reads — while the app read only
# ACCTOPS_SERVICE_TOKEN / CPA_EXPORT_TOKEN. Result: every app -> cpa-export
# request went out with NO Authorization header and the sidecar answered
# 401 "Missing bearer token", so the 1120 / 1120-S / 1065 export path was dead
# in production while healthchecks stayed green. Accepting the sidecar's own
# variable name as an alias makes the mismatch unrepresentable.
_CPA_EXPORT_BEARER_ENV_VARS: tuple[str, ...] = (
    "ACCTOPS_SERVICE_TOKEN",
    "CPA_EXPORT_TOKEN",
    "CPA_EXPORT_SERVICE_TOKEN",
)


def resolve_cpa_export_bearer() -> str | None:
    """Resolve the outbound bearer for the cpa-export sidecar.

    Returns the first non-empty value among _CPA_EXPORT_BEARER_ENV_VARS, or
    None when none is configured (callers then send no Authorization header,
    exactly as before — this helper never invents a credential).
    """
    for name in _CPA_EXPORT_BEARER_ENV_VARS:
        value = os.getenv(name)
        if value:
            return value
    return None


async def fetch_return_package(
    *,
    form_path: str,
    form_label: str,
    workspace_id: UUID,
    entity_id: UUID | str,
    tax_year: int,
    require_unscoped: bool = False,
) -> dict[str, Any]:
    """GET /exports/{form_path}/{tax_year} from the cpa-export sidecar.

    ``form_path`` is the URL segment ("1120s", "1065"); ``form_label`` is the
    human name used in error messages.
    """
    if require_unscoped:
        ctx = await authorize_workspace(
            workspace_id,
            entity_id,
            min_role="viewer",
            require_unscoped=True,
        )
    else:
        ctx = await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    entity_id_text = str(entity_id)
    is_consolidated = entity_id_text.strip().lower() in _CPA_EXPORT_CONSOLIDATED_ENTITY_IDS
    wire_entity_id = "all" if is_consolidated else entity_id_text
    assertion_entity_id = None if is_consolidated else entity_id_text

    settings = get_settings()
    auth = current_auth()

    headers: dict[str, str] = {}
    try:
        if auth is None:
            raise RuntimeError("authenticated caller context is unavailable")
        master_key = get_crypto().master_key_bytes()
        headers["X-Internal-Assertion"] = sign_assertion(
            master_key,
            uc_uid=auth.uc_uid,
            workspace_id=str(ctx.workspace_id),
            entity_id=assertion_entity_id,
            role=ctx.role,
            scopes=auth.scopes,
            agent_id=auth.agent_id,
        )
    except Exception as exc:  # noqa: BLE001 - the assertion boundary must fail closed
        failure_class = type(exc).__name__
        _log.error(
            "cpa-export identity assertion minting failed; failure_class=%s",
            failure_class,
        )
        allow_bearer_only = os.getenv(
            _CPA_EXPORT_ALLOW_BEARER_ONLY_ENV, ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not allow_bearer_only:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_CPA_EXPORT_IDENTITY_ASSERTION_ERROR,
            ) from exc

    service_token = resolve_cpa_export_bearer()
    if service_token:
        headers["Authorization"] = f"Bearer {service_token}"

    url = (
        f"{settings.cpa_export_url.rstrip('/')}/exports/{form_path}/{tax_year}"
        f"?workspace_id={ctx.workspace_id}&entity_id={wire_entity_id}"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=headers)

    if response.status_code == 400:
        raise ValueError(
            f"cpa-export rejected the {form_label} request: {response.text[:300]}"
        )
    if response.status_code == 404:
        raise LookupError(
            f"{form_label} export not found for entity {entity_id}, year "
            f"{tax_year}. Check the entity and year."
        )
    if response.status_code == 403:
        raise PermissionError(
            f"cpa-export service denied access to entity {entity_id}, year "
            f"{tax_year}."
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"cpa-export returned HTTP {response.status_code}: {response.text[:200]}"
        )

    data = response.json()
    data["_draft_notice"] = _DRAFT_NOTICE
    return data


__all__ = ("fetch_return_package", "resolve_cpa_export_bearer")

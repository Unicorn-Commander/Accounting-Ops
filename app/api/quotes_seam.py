"""Quotes seam HTTP routes (Customer-Ops ``quote.accepted`` → DRAFT paper).

  POST /api/workspaces/{wid}/entities/{eid}/quotes/accepted   — the seam (M2M or bookkeeper)
  GET  /api/workspaces/{wid}/invoice-schedules                — list (viewer)
  POST /api/workspaces/{wid}/invoice-schedules/run            — generate due drafts (bookkeeper)

The seam endpoint rides a WRITE variant of the trusted machine-to-machine rail
(``resolve_workspace_for_quotes_seam``). The existing federation dependency
(``resolve_workspace_for_federation``) is read-only by design: it floors every
trusted M2M caller to ``viewer``. This one is deliberately narrower in WHO and
wider in WHAT — it accepts exactly ONE actor and grants exactly ONE capability:

  * WHO:  a Keycloak client-credentials JWT (signature + issuer + expiry +
          audience verified by ``validate_bearer_token``) whose ``azp`` is in
          ``ACCTOPS_TRUSTED_M2M_CALLERS`` AND equals ``customer-ops`` AND that
          carries MACHINE proof (``AuthContext.m2m_proof ==
          "keycloak-client-credentials"``: no ``sid``/``session_state``,
          ``clientId`` == ``azp``, a ``service-account-customer-ops`` identity —
          see ``app.auth.keycloak_machine_proof``). A human who logged in
          through the customer-ops client carries the same ``azp`` and is
          refused (review H5). Any other trusted caller (tax-planning-ops, …)
          is refused — being on the allow-list is necessary, not sufficient.
          The Brigade-minted ``m2m:<client>`` door (``m2m_proof ==
          "brigade-actor"``) is EXCLUDED from this write route (review H6).
  * FENCE: ``QUOTES_ENABLED`` (config.quotes_enabled) must be on, else 404
          ``QUOTES_DISABLED``. With it off the whole seam is inert.
  * RE-GATE: the path workspace must exist + be active, must not be HIPAA, and
          when the token carries a ``workspace_id`` claim it must equal the path
          workspace (a token scoped to one workspace cannot federate another).
          The entity in the path must exist in that workspace (checked in the
          service, 404 otherwise).
  * WHAT: DRAFT documents only. The service never issues and never posts a
          journal entry, so the synthesized ``accountant`` role cannot reach
          the ledger through this route; no other route accepts this context.
  * AUDIT: ``created_by_uc_uid`` on every row is the synthetic ``m2m:customer-ops``.

A normal workspace member (accountant+; a service token / PAT with write
scope included) falls through to ``resolve_workspace`` unchanged.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from app.ar.invoicing import InvoiceError
from app.ar.quotes_seam import (
    QUOTES_SEAM_ACTOR,
    QuoteAcceptedPayload,
    QuoteSeamError,
    apply_quote_acceptance,
    generate_due_schedule_invoices,
    list_invoice_schedules,
    quotes_enabled,
)
from app.auth import AuthContext, validate_bearer_token
from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import (
    WorkspaceContext,
    _extract_workspace_id,
    _workspace_exists,
    require_role,
    resolve_workspace,
)
from app.workspace_session import workspace_session

_log = logging.getLogger("acctops.api.quotes_seam")

router = APIRouter(tags=["quotes-seam"])


async def resolve_workspace_for_quotes_seam(
    request: Request,
    auth: AuthContext = Depends(validate_bearer_token),
    workspace_id_header: str | None = Header(default=None, alias="X-Workspace-Id"),
) -> WorkspaceContext:
    """Write-capable M2M dependency for the quotes seam ONLY (see module docstring)."""
    if not auth.is_trusted_m2m:
        return await resolve_workspace(request, auth, workspace_id_header)

    if auth.m2m_proof != "keycloak-client-credentials":
        _log.warning(
            "[acctops.quotes_seam] refused trusted M2M caller %r without machine proof (door=%s)",
            auth.uc_uid,
            auth.m2m_proof or "keycloak-without-client-credentials-shape",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "quotes seam: a Keycloak client-credentials (machine) token is required; "
                "user sessions and Brigade-minted actors are refused on this write route"
            ),
        )

    expected_actor = f"m2m:{QUOTES_SEAM_ACTOR}"
    if auth.uc_uid != expected_actor:
        _log.warning(
            "[acctops.quotes_seam] refused trusted M2M actor %r (only %r may federate quotes)",
            auth.uc_uid,
            QUOTES_SEAM_ACTOR,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"quotes seam: only the '{QUOTES_SEAM_ACTOR}' actor may federate here",
        )

    workspace_id = _extract_workspace_id(request, workspace_id_header, auth.claims)
    claim_ws = auth.claims.get("workspace_id")
    if claim_ws and str(claim_ws) != str(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="workspace does not match the token's scoped workspace",
        )
    slug, hipaa = await _workspace_exists(workspace_id)
    if hipaa:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="HIPAA workspace: federation not permitted",
        )
    _log.info(
        "[acctops.quotes_seam] trusted M2M actor %r accepted for workspace %s (slug=%r)",
        auth.uc_uid,
        workspace_id,
        slug,
    )
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        role="accountant",  # this route creates DRAFTS only; no other route accepts this context
        user_uc_uid=auth.uc_uid,
        agent_id=QUOTES_SEAM_ACTOR,
        entity_scope=None,
        hipaa_protected=hipaa,
        pat_scopes=None,
    )


def _assert_enabled() -> None:
    """QUOTES_ENABLED fence (review L19): the seam is inert until an operator turns it on."""
    if not quotes_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "QUOTES_DISABLED", "reason": "quotes seam is disabled (set QUOTES_ENABLED=1)"},
        )


def _failed_detail(code: str, reason: str) -> dict[str, Any]:
    """Error body shape: the caller stores ``federated`` per A7."""
    return {
        "code": code,
        "reason": reason,
        "federated": {"accounting_ops": "failed", "partner": "skipped", "reason": reason},
    }


@router.post("/api/workspaces/{workspace_id}/entities/{entity_id}/quotes/accepted")
async def quote_accepted(
    workspace_id: UUID,
    entity_id: UUID,
    body: QuoteAcceptedPayload,
    response: Response,
    workspace: WorkspaceContext = Depends(resolve_workspace_for_quotes_seam),
) -> dict[str, Any]:
    """Create the DRAFT paper an accepted quote implies. Idempotent on quote_ref + version.

    201 when anything was created, 200 on a pure replay (same ids, nothing new).
    Never issues, never posts a journal entry. Errors carry
    ``{code, reason, federated}`` so Customer-Ops can record the adapter status.
    Role floor: bookkeeper (or the customer-ops M2M actor). Produces drafts only.
    """
    _assert_enabled()
    require_role(workspace, "bookkeeper")
    assert_entity_in_scope(workspace, entity_id)
    if body.vendor_entity_id:
        assert_entity_in_scope(workspace, body.vendor_entity_id)
    try:
        async with workspace_session(SessionLocal, workspace.workspace_id) as session:
            result = await apply_quote_acceptance(
                session,
                workspace_id=str(workspace.workspace_id),
                entity_id=str(entity_id),
                actor_uc_uid=workspace.user_uc_uid,
                payload=body,
            )
    except QuoteSeamError as exc:
        raise HTTPException(status_code=exc.http_status, detail=_failed_detail(exc.code, exc.reason)) from exc
    except InvoiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=_failed_detail("INVOICE_ERROR", str(exc))
        ) from exc
    response.status_code = status.HTTP_201_CREATED if result.get("created") else status.HTTP_200_OK
    return result


@router.get("/api/workspaces/{workspace_id}/invoice-schedules")
async def get_invoice_schedules(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    active_only: bool = False,
    limit: int = 200,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """List recurring invoice schedules. Role floor: viewer."""
    _assert_enabled()
    require_role(workspace, "viewer")
    scope = assert_entity_in_scope(workspace, entity_id)
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        rows = await list_invoice_schedules(
            session,
            workspace_id=str(workspace.workspace_id),
            entity_id=str(entity_id) if entity_id else None,
            entity_scope=scope,
            active_only=active_only,
            limit=limit,
        )
    return {"schedules": rows, "total": len(rows)}


@router.post("/api/workspaces/{workspace_id}/invoice-schedules/run")
async def run_invoice_schedules_route(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    as_of: date | None = None,
    limit: int = 200,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Generate the next DRAFT for every schedule due by ``as_of`` (default today).

    Idempotent per period; closed periods are skipped and reported. Never posts.
    Role floor: bookkeeper. Produces drafts only; never issues or posts.
    """
    _assert_enabled()
    require_role(workspace, "bookkeeper")
    scope = assert_entity_in_scope(workspace, entity_id)
    entity_ids: list[str | None]
    if entity_id is not None:
        entity_ids = [str(entity_id)]
    elif scope is not None:
        entity_ids = [str(e) for e in scope]
    else:
        entity_ids = [None]
    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    partner_bills: list[dict[str, Any]] = []
    partner_skipped: list[dict[str, Any]] = []
    due = 0
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        for eid in entity_ids:
            res = await generate_due_schedule_invoices(
                session,
                workspace_id=str(workspace.workspace_id),
                actor_uc_uid=workspace.user_uc_uid,
                as_of=as_of,
                entity_id=eid,
                limit=limit,
            )
            due += res["schedules_due"]
            generated.extend(res["generated"])
            skipped.extend(res["skipped"])
            partner_bills.extend(res["partner_bills"])
            partner_skipped.extend(res["partner_skipped"])
    return {
        "as_of": (as_of or date.today()).isoformat(),
        "schedules_due": due,
        "generated": generated,
        "skipped": skipped,
        "partner_bills": partner_bills,
        "partner_skipped": partner_skipped,
    }

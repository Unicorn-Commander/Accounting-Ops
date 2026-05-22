"""HTTP endpoints for credential capture + management.

  POST   /api/workspaces/{wid}/credentials                          generic store
  POST   /api/workspaces/{wid}/credentials/simplefin/claim          one-shot SimpleFIN setup-token claim + store
  GET    /api/workspaces/{wid}/credentials                          metadata-only list
  DELETE /api/workspaces/{wid}/credentials/{cred_id}                remove

For SimpleFIN specifically the user pastes the base64-encoded setup token; the
server decodes, POSTs to the one-time claim URL, gets back the permanent access
URL, encrypts the access URL into tenant_credentials. The setup token is
single-use so the server must be the one that claims it (not the browser).
"""
from __future__ import annotations

import base64
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.db import SessionLocal
from app.security.credentials import (
    delete_credential,
    list_credentials,
    store_credential,
)
from app.tenancy import WorkspaceContext, require_role, resolve_workspace

router = APIRouter(tags=["credentials"])


class StoreCredentialRequest(BaseModel):
    connector: str = Field(..., min_length=1, max_length=96)
    label: str = Field(..., min_length=1, max_length=256)
    plaintext_value: str = Field(..., min_length=1)
    expires_at: str | None = None


class ClaimSimplefinRequest(BaseModel):
    setup_token: str = Field(..., min_length=10, max_length=4096)
    label: str = Field(default="SimpleFIN")


def _is_valid_simplefin_access_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        if parts.scheme != "https":
            return False
        if not parts.username or not parts.password:
            return False
        return True
    except Exception:
        return False


@router.post("/api/workspaces/{workspace_id}/credentials")
async def http_store_credential(
    workspace_id: UUID,
    body: StoreCredentialRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    require_role(workspace, "admin")
    async with SessionLocal() as session:
        async with session.begin():
            cred_id = await store_credential(
                session,
                workspace_id,
                body.connector,
                body.label,
                body.plaintext_value,
                created_by_uc_uid=workspace.user_uc_uid,
                expires_at=body.expires_at,
            )
    return {"id": str(cred_id), "connector": body.connector, "label": body.label}


@router.post("/api/workspaces/{workspace_id}/credentials/simplefin/claim")
async def http_claim_simplefin(
    workspace_id: UUID,
    body: ClaimSimplefinRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Claim a SimpleFIN setup token and store the resulting access URL encrypted.

    The setup token is base64-encoded. Decode it, POST to the resulting URL
    with an empty body, capture the access URL from the response body.
    The setup token is single-use so we ONLY do this once per token.
    """
    require_role(workspace, "admin")
    # Decode the setup token.
    try:
        claim_url = base64.b64decode(body.setup_token).decode("utf-8").strip()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Setup token is not valid base64",
        ) from exc
    if not claim_url.startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Decoded setup token does not look like a SimpleFIN claim URL",
        )

    # Claim the URL. Single-use; SimpleFIN responds with the permanent access URL.
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(claim_url, headers={"Content-Length": "0"})
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"SimpleFIN claim request failed: {type(exc).__name__}",
        ) from exc

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"SimpleFIN rejected the claim (HTTP {resp.status_code}). "
                "Likely already claimed or expired — generate a new setup "
                "token at beta-bridge.simplefin.org."
            ),
        )

    access_url = resp.text.strip()
    if not _is_valid_simplefin_access_url(access_url):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="SimpleFIN response did not look like an access URL",
        )

    # Store encrypted.
    async with SessionLocal() as session:
        async with session.begin():
            cred_id = await store_credential(
                session,
                workspace_id,
                "simplefin",
                body.label,
                access_url,
                created_by_uc_uid=workspace.user_uc_uid,
            )

    # Test the access URL works by fetching a balances-only response.
    parts = urlsplit(access_url)
    user = parts.username or ""
    pw = parts.password or ""
    host = parts.hostname or ""
    path = parts.path.rstrip("/")
    if not path.endswith("/accounts"):
        path = f"{path}/accounts"
    accounts_count = 0
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            verify_resp = await client.get(
                f"{parts.scheme}://{host}{path}?balances-only=1",
                auth=(user, pw),
            )
            if verify_resp.status_code < 400:
                payload = verify_resp.json()
                accounts_count = len(payload.get("accounts", []))
    except Exception:
        # Don't fail the claim just because the verify-fetch didn't work; the
        # credential is stored and the user can retry from the dashboard.
        pass

    return {
        "id": str(cred_id),
        "connector": "simplefin",
        "label": body.label,
        "accounts_count": accounts_count,
        "verified": accounts_count > 0,
    }


@router.get("/api/workspaces/{workspace_id}/credentials")
async def http_list_credentials(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> list[dict]:
    async with SessionLocal() as session:
        async with session.begin():
            metas = await list_credentials(session, workspace_id)
    return [
        {
            "id": str(m.id),
            "connector": m.connector,
            "label": m.label,
            "key_version": m.key_version,
            "encrypted_at": m.encrypted_at,
            "last_used_at": m.last_used_at,
            "expires_at": m.expires_at,
        }
        for m in metas
    ]


@router.delete(
    "/api/workspaces/{workspace_id}/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def http_delete_credential(
    workspace_id: UUID,
    credential_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> None:
    require_role(workspace, "admin")
    async with SessionLocal() as session:
        async with session.begin():
            removed = await delete_credential(
                session,
                workspace_id,
                credential_id,
                actor_uc_uid=workspace.user_uc_uid,
            )
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

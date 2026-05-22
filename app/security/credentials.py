"""High-level credentials store. Wraps the crypto module + tenant_credentials
table with sane store/retrieve/delete semantics and access-log auditing.

Used by Phase 1.6 connector endpoints (cash_position, transactions, future
eBay + IMAP) so that no env var ever holds a long-lived credential.

Access pattern:
    # Store at connector-configuration time (one-shot, never returns plaintext)
    cred_id = await store_credential(
        session, workspace_id, "simplefin", "BofA+USAA", access_url,
        created_by_uc_uid=auth.uc_uid,
    )

    # Decrypt at use time (e.g., inside cash_position handler)
    plaintext = await get_credential_plaintext(
        session, workspace_id, "simplefin",
        requesting_service="cash_position",
        actor_uc_uid=auth.uc_uid,
    )
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.crypto import (
    KEY_VERSION,
    CredentialDecryptionError,
    get_crypto,
)


@dataclass(frozen=True)
class CredentialMetadata:
    id: uuid.UUID
    workspace_id: uuid.UUID
    connector: str
    label: str
    key_version: int
    encrypted_at: str
    last_used_at: str | None
    expires_at: str | None


class CredentialNotFound(LookupError):
    pass


async def store_credential(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    connector: str,
    label: str,
    plaintext_value: str,
    *,
    created_by_uc_uid: str | None = None,
    expires_at: str | None = None,
) -> uuid.UUID:
    """Encrypt + persist a credential. Returns the new cred_id.

    Idempotent on (workspace_id, connector, label): if a credential with the
    same triple exists, it's overwritten in place with a fresh nonce.
    """
    crypto = get_crypto()
    # Reserve a cred_id first so we can use it as part of the AAD.
    cred_id = uuid.uuid4()
    nonce, ciphertext = crypto.encrypt(plaintext_value, workspace_id, cred_id)

    # ON CONFLICT updates existing row; cred_id changes only on fresh insert.
    # On overwrite we keep the original id so callers' references stay valid.
    result = await session.execute(
        text(
            """
            INSERT INTO public.tenant_credentials (
                id, workspace_id, connector, label,
                ciphertext, nonce, key_version,
                created_by_uc_uid, encrypted_at, expires_at
            )
            VALUES (
                :id, :workspace_id, :connector, :label,
                :ciphertext, :nonce, :key_version,
                :created_by_uc_uid, now(), :expires_at
            )
            ON CONFLICT (workspace_id, connector, label) DO UPDATE
            SET ciphertext = EXCLUDED.ciphertext,
                nonce = EXCLUDED.nonce,
                key_version = EXCLUDED.key_version,
                encrypted_at = now(),
                expires_at = EXCLUDED.expires_at
            RETURNING id
            """
        ),
        {
            "id": str(cred_id),
            "workspace_id": str(workspace_id),
            "connector": connector,
            "label": label,
            "ciphertext": ciphertext,
            "nonce": nonce,
            "key_version": KEY_VERSION,
            "created_by_uc_uid": created_by_uc_uid,
            "expires_at": expires_at,
        },
    )
    final_cred_id = uuid.UUID(str(result.scalar_one()))

    # If ON CONFLICT updated an existing row, the cred_id in the AAD doesn't
    # match the stored row id. Re-encrypt with the correct cred_id and update.
    if final_cred_id != cred_id:
        nonce2, ciphertext2 = crypto.encrypt(
            plaintext_value, workspace_id, final_cred_id
        )
        await session.execute(
            text(
                """
                UPDATE public.tenant_credentials
                SET ciphertext = :ciphertext, nonce = :nonce, encrypted_at = now()
                WHERE id = :id
                """
            ),
            {
                "ciphertext": ciphertext2,
                "nonce": nonce2,
                "id": str(final_cred_id),
            },
        )

    await _log(
        session,
        workspace_id=workspace_id,
        credential_id=final_cred_id,
        actor_uc_uid=created_by_uc_uid,
        actor_agent_id=None,
        requesting_service="credentials_api",
        operation="encrypt",
        success=True,
    )
    return final_cred_id


async def get_credential_plaintext(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    connector: str,
    *,
    label: str | None = None,
    requesting_service: str,
    actor_uc_uid: str | None = None,
    actor_agent_id: str | None = None,
) -> str | None:
    """Decrypt the active credential for a workspace + connector.

    If `label` is given, scopes to that specific row; otherwise returns the
    most-recently-encrypted credential of that connector for the workspace.
    Returns None if no credential exists (caller decides whether that's an
    error or expected empty state).
    """
    where = "workspace_id = :workspace_id AND connector = :connector"
    params: dict[str, Any] = {
        "workspace_id": str(workspace_id),
        "connector": connector,
    }
    if label is not None:
        where += " AND label = :label"
        params["label"] = label

    row = (
        await session.execute(
            text(
                f"""
                SELECT id, ciphertext, nonce, key_version
                FROM public.tenant_credentials
                WHERE {where}
                ORDER BY encrypted_at DESC
                LIMIT 1
                """
            ),
            params,
        )
    ).first()
    if row is None:
        return None
    cred_id = uuid.UUID(str(row.id))
    crypto = get_crypto()
    try:
        plaintext = crypto.decrypt(
            bytes(row.nonce),
            bytes(row.ciphertext),
            workspace_id,
            cred_id,
        )
    except CredentialDecryptionError:
        await _log(
            session,
            workspace_id=workspace_id,
            credential_id=cred_id,
            actor_uc_uid=actor_uc_uid,
            actor_agent_id=actor_agent_id,
            requesting_service=requesting_service,
            operation="decrypt",
            success=False,
        )
        raise

    # Bump last_used_at and audit.
    await session.execute(
        text(
            "UPDATE public.tenant_credentials SET last_used_at = now() WHERE id = :id"
        ),
        {"id": str(cred_id)},
    )
    await _log(
        session,
        workspace_id=workspace_id,
        credential_id=cred_id,
        actor_uc_uid=actor_uc_uid,
        actor_agent_id=actor_agent_id,
        requesting_service=requesting_service,
        operation="decrypt",
        success=True,
    )
    return plaintext


async def list_credentials(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[CredentialMetadata]:
    """Metadata-only listing. Never returns ciphertext or plaintext."""
    rows = (
        await session.execute(
            text(
                """
                SELECT id, workspace_id, connector, label, key_version,
                       encrypted_at, last_used_at, expires_at
                FROM public.tenant_credentials
                WHERE workspace_id = :workspace_id
                ORDER BY connector, label
                """
            ),
            {"workspace_id": str(workspace_id)},
        )
    ).mappings().all()
    return [
        CredentialMetadata(
            id=uuid.UUID(str(r["id"])),
            workspace_id=uuid.UUID(str(r["workspace_id"])),
            connector=r["connector"],
            label=r["label"],
            key_version=r["key_version"],
            encrypted_at=str(r["encrypted_at"]),
            last_used_at=str(r["last_used_at"]) if r["last_used_at"] else None,
            expires_at=str(r["expires_at"]) if r["expires_at"] else None,
        )
        for r in rows
    ]


async def delete_credential(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    credential_id: uuid.UUID,
    *,
    actor_uc_uid: str | None = None,
) -> bool:
    """Delete by (workspace_id, credential_id). Returns True if a row was removed."""
    result = await session.execute(
        text(
            """
            DELETE FROM public.tenant_credentials
            WHERE id = :id AND workspace_id = :workspace_id
            """
        ),
        {"id": str(credential_id), "workspace_id": str(workspace_id)},
    )
    removed = (result.rowcount or 0) > 0
    if removed:
        await _log(
            session,
            workspace_id=workspace_id,
            credential_id=credential_id,
            actor_uc_uid=actor_uc_uid,
            actor_agent_id=None,
            requesting_service="credentials_api",
            operation="delete",
            success=True,
        )
    return removed


async def _log(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    credential_id: uuid.UUID,
    actor_uc_uid: str | None,
    actor_agent_id: str | None,
    requesting_service: str,
    operation: str,
    success: bool,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO public.credential_access_log (
                workspace_id, credential_id, actor_uc_uid, actor_agent_id,
                requesting_service, operation, success
            )
            VALUES (
                :workspace_id, :credential_id, :actor_uc_uid, :actor_agent_id,
                :requesting_service, :operation, :success
            )
            """
        ),
        {
            "workspace_id": str(workspace_id),
            "credential_id": str(credential_id),
            "actor_uc_uid": actor_uc_uid,
            "actor_agent_id": actor_agent_id,
            "requesting_service": requesting_service,
            "operation": operation,
            "success": success,
        },
    )

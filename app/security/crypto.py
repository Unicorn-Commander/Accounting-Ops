"""Envelope encryption for tenant credentials.

Design (per docs/PHASE-1-6-ENCRYPTED-CREDENTIALS.md):
- Master key: 32 random bytes from a Docker-secret file, mounted at
  /run/secrets/accounting_ops_master_key. App refuses to start without it.
- Per-tenant DEK: HKDF-SHA256(master_key, info='acctops:tenant-dek:v1:' + tenant_uuid).
- AEAD: AES-256-GCM with 96-bit random nonce. AAD = tenant_id || cred_id so
  ciphertext copied across rows fails authentication.
- Versioned keys for rotation (KEY_VERSION column). Lazy re-encrypt on read.

The crypto operations are CPU-bound and microsecond-fast; don't push them into
run_in_executor. Just call them from async handlers.
"""
from __future__ import annotations

import os
import secrets
import uuid
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MASTER_KEY_PATH = Path(
    os.getenv("ACCTOPS_MASTER_KEY_PATH", "/run/secrets/accounting_ops_master_key")
)
KEY_VERSION = 1


class CryptoNotInitializedError(RuntimeError):
    """Raised when the master key isn't available at startup."""


class CredentialDecryptionError(PermissionError):
    """Generic failure on decrypt. Never includes details about what went wrong
    (would leak a tamper-vs-AAD-mismatch oracle to attackers)."""


class CredCrypto:
    """Envelope encryption manager.

    Usage:
        crypto = get_crypto()
        nonce, ciphertext = crypto.encrypt("secret-value", workspace_uuid, cred_uuid)
        plaintext = crypto.decrypt(nonce, ciphertext, workspace_uuid, cred_uuid)
    """

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise ValueError(
                f"master key must be exactly 32 bytes (got {len(master_key)})"
            )
        # Bytes-only; never log or repr this object.
        self._master_key = master_key

    def master_key_bytes(self) -> bytes:
        """Return the raw master key bytes for HKDF-derived subkeys.

        Intended for in-process callers that need a per-purpose subkey (e.g.
        ``app.security.internal_assertion`` which derives an
        assertion-signing key via HKDF info='acctops:internal-assertion:v1').
        NEVER log this value, NEVER ship it over the network, NEVER persist
        it outside of the original secret file. Per-purpose HKDF derivation
        means a leak of any derived key does NOT expose the master key.
        """
        return self._master_key

    @classmethod
    def from_file(cls, path: Path = MASTER_KEY_PATH) -> "CredCrypto":
        """Load the master key from a secret file. Refuses to auto-generate."""
        if not path.exists():
            raise CryptoNotInitializedError(
                f"Master key file missing at {path}. Bootstrap with "
                "scripts/bootstrap_master_key.py before starting the app. "
                "DO NOT auto-generate — it would orphan any existing ciphertext."
            )
        raw = path.read_bytes()
        # File may have a trailing newline if someone wrote it with echo/cat.
        # Strip a single trailing 0x0a (LF) but NOT internal bytes.
        if len(raw) == 33 and raw[-1] == 0x0A:
            raw = raw[:-1]
        return cls(raw)

    def _tenant_dek(self, tenant_id: uuid.UUID) -> bytes:
        """Derive a deterministic per-tenant DEK via HKDF-SHA256."""
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"acctops:tenant-dek:v1:" + tenant_id.bytes,
        ).derive(self._master_key)

    def encrypt(
        self,
        plaintext: str,
        tenant_id: uuid.UUID,
        cred_id: uuid.UUID,
    ) -> tuple[bytes, bytes]:
        """Encrypt plaintext.

        Returns (nonce, ciphertext_with_tag).
        - nonce: 12 bytes, random per-encrypt
        - ciphertext_with_tag: AESGCM output (ciphertext || 16-byte auth tag)
        AAD = tenant_id.bytes || cred_id.bytes so ciphertext copied between
        rows fails authentication.
        """
        dek = self._tenant_dek(tenant_id)
        nonce = secrets.token_bytes(12)
        aad = tenant_id.bytes + cred_id.bytes
        cipher = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), aad)
        return nonce, cipher

    def decrypt(
        self,
        nonce: bytes,
        ciphertext: bytes,
        tenant_id: uuid.UUID,
        cred_id: uuid.UUID,
    ) -> str:
        """Decrypt a credential.

        Raises CredentialDecryptionError (generic) on any failure. Never logs
        the underlying exception — would leak a tampering oracle.
        """
        dek = self._tenant_dek(tenant_id)
        aad = tenant_id.bytes + cred_id.bytes
        try:
            plain = AESGCM(dek).decrypt(nonce, ciphertext, aad)
        except Exception:  # InvalidTag or anything else
            raise CredentialDecryptionError(
                "credential decryption failed (tampered, wrong AAD, or wrong key)"
            ) from None
        return plain.decode("utf-8")


@lru_cache(maxsize=1)
def get_crypto() -> CredCrypto:
    """Singleton accessor. Lazy-load on first call so test code can override
    MASTER_KEY_PATH via env var without a module-level import side effect.
    """
    return CredCrypto.from_file()


def reset_crypto_cache() -> None:
    """Test helper. Clears the lru_cache so the master key is re-read."""
    get_crypto.cache_clear()

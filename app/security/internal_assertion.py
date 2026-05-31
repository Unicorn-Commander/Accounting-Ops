"""Signed internal assertion for sidecar service-to-service authorization.

Audit 2026-05-31 Bug 10. Previously, when ``get_form_1120_data`` (or any MCP
tool whose execution requires the cpa-export sidecar) forwarded a request, the
sidecar received only an ``ACCTOPS_SERVICE_TOKEN``-derived bearer and could not
validate the actual caller's identity, workspace, entity, or role. The audit
called this a service-token-impersonation gap: the sidecar trusted the static
token, the static token's permissions were potentially broader than the
caller's, and the BOLA control therefore depended on every tool getting the
authorize_workspace check exactly right BEFORE the sidecar call.

Fix: option (b) — at the moment the tool has finished its local authorization,
mint a short-lived (5-minute) signed assertion bound to the caller's
workspace_id, entity_id, uc_uid, role, and scopes. Send it as
``X-Internal-Assertion`` to the sidecar. The sidecar verifies the signature
against the same master key and uses the assertion's identity for its own
authorization. The static service token remains as a transport-level handshake
(it gates who can even hit the sidecar at all), but the per-call identity now
comes from a tamper-evident assertion.

Constant-time comparison on the signature; signed-payload format keeps the
caller's identity in the wire format the sidecar reads, not in a header it
trusts blindly.

Format:
    <b64url(payload_json)>.<b64url(hmac_sha256(master_key_subkey, payload))>

Where ``payload_json`` is::

    {
      "iss": "accounting-ops",
      "aud": "cpa-export",
      "iat": <unix>,
      "exp": <unix>,            # always iat + 300 (5 min ceiling)
      "uc_uid": "<caller>",
      "workspace_id": "<uuid>",
      "entity_id":   "<uuid> or 'consolidated' or null",
      "role":        "viewer|accountant|admin|owner|...",
      "scopes":      ["service", "..."],
      "agent_id":    "<rfc8693 act sub> or null",
      "jti":         "<random 16 bytes hex>"  # replay-defense aid
    }

A separate HKDF-derived subkey ("acctops:internal-assertion:v1") is used so a
master-key compromise that already exposes credential ciphertext doesn't
re-use the same DEK; rotating the master key rotates the assertion key.
"""
from __future__ import annotations

import base64
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


# Identity fields the assertion carries. Kept dataclass-shaped so the sidecar
# can hydrate into a typed object on its end and so callers can't accidentally
# pass un-vetted dicts straight from the agent input.
@dataclass(frozen=True)
class InternalAssertion:
    uc_uid: str
    workspace_id: str
    entity_id: str | None
    role: str
    scopes: tuple[str, ...]
    agent_id: str | None
    iat: int
    exp: int
    jti: str
    iss: str = "accounting-ops"
    aud: str = "cpa-export"


# Hard ceiling on assertion lifetime. The signing path enforces this — a caller
# can't request a longer-lived assertion. Verifier additionally rejects an exp
# more than this in the future so a forged assertion can't outlive the policy
# (defense in depth — the signature must match anyway).
_MAX_LIFETIME_SECONDS = 300  # 5 minutes per audit recommendation
_DEFAULT_LIFETIME_SECONDS = 300


class AssertionError(PermissionError):
    """Verification failure. Generic by design — no oracle for tampering vs
    expiry vs audience mismatch. The sidecar logs the underlying reason
    server-side but returns the same 401 to the caller no matter what."""


def _derive_signing_key(master_key: bytes) -> bytes:
    """Per-purpose HKDF subkey so the master key isn't reused as a MAC key.

    A future v2 (e.g. swap to Ed25519) can change the HKDF info label and have
    the sidecar accept both during rollover.
    """
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"acctops:internal-assertion:v1",
    ).derive(master_key)


def _b64url_encode(data: bytes) -> str:
    """URL-safe base64 with stripped padding (JWT-style; smaller wire bytes)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad_needed = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad_needed))


def _canonical_payload(
    *,
    uc_uid: str,
    workspace_id: str,
    entity_id: str | None,
    role: str,
    scopes: tuple[str, ...],
    agent_id: str | None,
    iat: int,
    exp: int,
    jti: str,
) -> bytes:
    """Stable JSON encoding — sort_keys + no whitespace — so signature is
    deterministic across implementations (Python json sorts otherwise vary)."""
    payload: dict[str, Any] = {
        "iss": "accounting-ops",
        "aud": "cpa-export",
        "iat": iat,
        "exp": exp,
        "uc_uid": uc_uid,
        "workspace_id": workspace_id,
        "entity_id": entity_id,
        "role": role,
        "scopes": list(scopes),
        "agent_id": agent_id,
        "jti": jti,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_assertion(
    master_key: bytes,
    *,
    uc_uid: str,
    workspace_id: str,
    entity_id: str | None,
    role: str,
    scopes: tuple[str, ...] | list[str] | None = None,
    agent_id: str | None = None,
    lifetime_seconds: int = _DEFAULT_LIFETIME_SECONDS,
    _now_override: int | None = None,
) -> str:
    """Mint a signed assertion for sidecar use. Returns the wire string.

    Hard-caps the lifetime at _MAX_LIFETIME_SECONDS (5 min) per the audit's
    recommendation. ``scopes`` is normalised to a tuple; ``agent_id`` is the
    RFC 8693 ``act.sub`` if present.
    """
    if not uc_uid or not workspace_id or not role:
        raise ValueError(
            "sign_assertion requires uc_uid, workspace_id, and role"
        )
    if lifetime_seconds <= 0 or lifetime_seconds > _MAX_LIFETIME_SECONDS:
        lifetime_seconds = _DEFAULT_LIFETIME_SECONDS
    scope_tuple = tuple(scopes) if scopes else ()
    now = _now_override if _now_override is not None else int(time.time())
    iat = now
    exp = now + lifetime_seconds
    jti = secrets.token_hex(16)

    payload_bytes = _canonical_payload(
        uc_uid=uc_uid,
        workspace_id=workspace_id,
        entity_id=entity_id,
        role=role,
        scopes=scope_tuple,
        agent_id=agent_id,
        iat=iat,
        exp=exp,
        jti=jti,
    )
    signing_key = _derive_signing_key(master_key)
    sig = hmac.new(signing_key, payload_bytes, hashes.SHA256().name).digest()
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(sig)}"


def verify_assertion(
    master_key: bytes,
    token: str,
    *,
    expected_audience: str = "cpa-export",
    expected_issuer: str = "accounting-ops",
    leeway_seconds: int = 30,
    _now_override: int | None = None,
) -> InternalAssertion:
    """Verify the assertion + return the typed identity. Raises AssertionError
    on ANY failure (malformed, bad signature, expired, wrong audience).

    Uses ``hmac.compare_digest`` for the signature comparison so timing leaks
    don't reveal which prefix bytes match.
    """
    if not isinstance(token, str) or token.count(".") != 1:
        raise AssertionError("Malformed assertion (must be <payload>.<sig>)")
    payload_b64, sig_b64 = token.split(".", 1)
    try:
        payload_bytes = _b64url_decode(payload_b64)
        provided_sig = _b64url_decode(sig_b64)
    except (ValueError, TypeError) as exc:
        raise AssertionError("Malformed assertion encoding") from exc

    signing_key = _derive_signing_key(master_key)
    expected_sig = hmac.new(signing_key, payload_bytes, hashes.SHA256().name).digest()
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise AssertionError("Bad assertion signature")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AssertionError("Malformed assertion payload") from exc
    if not isinstance(payload, dict):
        raise AssertionError("Malformed assertion payload (not an object)")

    iss = payload.get("iss")
    aud = payload.get("aud")
    iat = payload.get("iat")
    exp = payload.get("exp")
    uc_uid = payload.get("uc_uid")
    workspace_id = payload.get("workspace_id")
    entity_id = payload.get("entity_id")
    role = payload.get("role")
    scopes = payload.get("scopes") or []
    agent_id = payload.get("agent_id")
    jti = payload.get("jti")

    if iss != expected_issuer:
        raise AssertionError("Wrong issuer")
    if aud != expected_audience:
        raise AssertionError("Wrong audience")
    if not isinstance(uc_uid, str) or not uc_uid:
        raise AssertionError("Missing uc_uid")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise AssertionError("Missing workspace_id")
    if not isinstance(role, str) or not role:
        raise AssertionError("Missing role")
    if not isinstance(iat, int) or not isinstance(exp, int):
        raise AssertionError("Invalid iat/exp")
    if not isinstance(jti, str) or not jti:
        raise AssertionError("Missing jti")
    if not isinstance(scopes, list):
        raise AssertionError("Invalid scopes")

    now = _now_override if _now_override is not None else int(time.time())
    if exp + leeway_seconds < now:
        raise AssertionError("Assertion expired")
    # Defense in depth: reject an assertion that claims a lifetime far longer
    # than policy. A correctly-signed assertion must obey the 5-minute ceiling
    # — if exp - iat > _MAX_LIFETIME_SECONDS, the signing side is malicious
    # or bug-compromised. (Signature already validated; this is a paranoia
    # check against a key-leak forger.)
    if exp - iat > _MAX_LIFETIME_SECONDS + leeway_seconds:
        raise AssertionError("Assertion lifetime exceeds policy")
    if iat - leeway_seconds > now:
        # Issued in the future — clock skew beyond leeway. Reject.
        raise AssertionError("Assertion not yet valid (iat in the future)")

    if entity_id is not None and not isinstance(entity_id, str):
        raise AssertionError("Invalid entity_id")
    if agent_id is not None and not isinstance(agent_id, str):
        raise AssertionError("Invalid agent_id")

    return InternalAssertion(
        uc_uid=uc_uid,
        workspace_id=workspace_id,
        entity_id=entity_id,
        role=role,
        scopes=tuple(str(s) for s in scopes),
        agent_id=agent_id,
        iat=iat,
        exp=exp,
        jti=jti,
        iss=iss,
        aud=aud,
    )


__all__ = (
    "InternalAssertion",
    "AssertionError",
    "sign_assertion",
    "verify_assertion",
)

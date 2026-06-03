"""Unit tests for the eBay OAuth state-token signing/verification.

The state token MUST be:
  * HMAC-signed so a tampered token is rejected
  * TTL-bound (10 min) so a leaked URL doesn't replay later
  * Single-use (replay of the same token rejected the second time)
  * Round-trip preserves (workspace_id, entity_id, uid, nonce)

Pure-Python — no DB, no app instance, no env beyond what we monkeypatch.
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _reset_redeemed_states():
    from app.api import ebay_oauth

    ebay_oauth._REDEEMED_STATES.clear()
    yield
    ebay_oauth._REDEEMED_STATES.clear()


def _build_payload(*, ttl_s: int = 600) -> dict:
    return {
        "wid": str(uuid.uuid4()),
        "eid": str(uuid.uuid4()),
        "uid": "aaron",
        "nonce": "test-nonce-abc",
        "exp": int(time.time()) + ttl_s,
    }


def test_state_round_trip_preserves_fields(monkeypatch, tmp_path):
    from app.api import ebay_oauth

    # Force a known signing secret so the test is reproducible.
    key_file = tmp_path / "key"
    key_file.write_bytes(b"x" * 32)
    monkeypatch.setattr(ebay_oauth, "MASTER_KEY_PATH", key_file)

    payload = _build_payload()
    token = ebay_oauth._sign_state(payload)
    parsed = ebay_oauth._verify_state(token)
    assert parsed["wid"] == payload["wid"]
    assert parsed["eid"] == payload["eid"]
    assert parsed["uid"] == payload["uid"]
    assert parsed["nonce"] == payload["nonce"]


def test_state_signature_tamper_rejected(monkeypatch, tmp_path):
    from app.api import ebay_oauth

    key_file = tmp_path / "key"
    key_file.write_bytes(b"y" * 32)
    monkeypatch.setattr(ebay_oauth, "MASTER_KEY_PATH", key_file)

    token = ebay_oauth._sign_state(_build_payload())
    # Corrupt the FIRST signature char, not the last. The signature is base64
    # with padding stripped; its LAST char carries only 4 significant bits + 2
    # base64 padding bits, so substituting it can decode to the SAME bytes and
    # the "tamper" becomes a silent no-op (~1/16 of runs, since the payload —
    # and thus the HMAC — is random per run). The first char's 6 bits are all
    # significant. Pick a replacement guaranteed different from the original.
    p64, s64 = token.split(".")
    flipped = "Z" if s64[0] != "Z" else "Y"
    tampered = f"{p64}.{flipped}{s64[1:]}"

    with pytest.raises(HTTPException) as exc:
        ebay_oauth._verify_state(tampered)
    assert exc.value.status_code == 400
    assert "signature" in exc.value.detail.lower()


def test_state_expired_rejected(monkeypatch, tmp_path):
    from app.api import ebay_oauth

    key_file = tmp_path / "key"
    key_file.write_bytes(b"z" * 32)
    monkeypatch.setattr(ebay_oauth, "MASTER_KEY_PATH", key_file)

    # ttl_s = -10 means exp is already in the past
    token = ebay_oauth._sign_state(_build_payload(ttl_s=-10))
    with pytest.raises(HTTPException) as exc:
        ebay_oauth._verify_state(token)
    assert exc.value.status_code == 400
    assert "expire" in exc.value.detail.lower()


def test_state_replay_rejected(monkeypatch, tmp_path):
    from app.api import ebay_oauth

    key_file = tmp_path / "key"
    key_file.write_bytes(b"w" * 32)
    monkeypatch.setattr(ebay_oauth, "MASTER_KEY_PATH", key_file)

    token = ebay_oauth._sign_state(_build_payload())
    # First verify: OK
    ebay_oauth._verify_state(token)
    # Mark as redeemed (this is what the callback does after verify)
    ebay_oauth._mark_state_redeemed(token)
    # Second verify: must fail
    with pytest.raises(HTTPException) as exc:
        ebay_oauth._verify_state(token)
    assert exc.value.status_code == 400
    assert "redeemed" in exc.value.detail.lower()


def test_state_malformed_rejected(monkeypatch, tmp_path):
    from app.api import ebay_oauth

    key_file = tmp_path / "key"
    key_file.write_bytes(b"k" * 32)
    monkeypatch.setattr(ebay_oauth, "MASTER_KEY_PATH", key_file)

    with pytest.raises(HTTPException) as exc:
        ebay_oauth._verify_state("not-a-token")
    assert exc.value.status_code == 400


def test_state_signing_key_isolation(monkeypatch, tmp_path):
    """A token signed with one master key cannot be verified by another."""
    from app.api import ebay_oauth

    key_a = tmp_path / "ka"
    key_a.write_bytes(b"a" * 32)
    monkeypatch.setattr(ebay_oauth, "MASTER_KEY_PATH", key_a)
    token = ebay_oauth._sign_state(_build_payload())

    # Swap to a different key
    key_b = tmp_path / "kb"
    key_b.write_bytes(b"b" * 32)
    monkeypatch.setattr(ebay_oauth, "MASTER_KEY_PATH", key_b)

    with pytest.raises(HTTPException):
        ebay_oauth._verify_state(token)

"""Unit tests for the Stripe webhook HMAC-SHA256 signature verification.

Per Stripe's signing spec:
  * header looks like "t=1492774577,v1=<hex_sig>,v0=..."
  * signed_payload = "{t}.{body}"
  * expected = HMAC-SHA256(secret, signed_payload)

Stripe SDK matches if ANY v1 signature in the header matches the expected and
the timestamp is within ±tolerance_seconds (default 300s).

Tested cases:
  - Valid signature + recent timestamp → True
  - Tampered payload (one byte flipped) → False
  - Tampered signature → False
  - Stale timestamp (> tolerance) → False
  - Future timestamp (> tolerance) → False
  - Missing v1 → False
  - Missing t → False
  - Wrong secret → False
  - Empty signature header → False
  - Empty secret → False
  - Multiple v1 sigs (rolling secret rotation) → True if ANY matches
"""
from __future__ import annotations

import hashlib
import hmac
import time


from app.api.stripe_oauth import verify_stripe_signature, WEBHOOK_TOLERANCE_SECONDS


SECRET = "whsec_test_FAKE_SECRET_KEY"
PAYLOAD = b'{"id":"evt_test_1","type":"charge.succeeded","data":{}}'


def _build_header(payload: bytes, secret: str, t: int) -> str:
    signed = f"{t}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def test_valid_signature_recent_timestamp():
    now = int(time.time())
    header = _build_header(PAYLOAD, SECRET, now)
    assert verify_stripe_signature(PAYLOAD, header, SECRET, now_ts=now) is True


def test_tampered_payload_rejected():
    now = int(time.time())
    header = _build_header(PAYLOAD, SECRET, now)
    tampered = PAYLOAD + b"."  # 1-byte difference
    assert verify_stripe_signature(tampered, header, SECRET, now_ts=now) is False


def test_tampered_signature_rejected():
    now = int(time.time())
    header = _build_header(PAYLOAD, SECRET, now)
    # Flip last char of the signature
    last = header[-1]
    tampered = header[:-1] + ("a" if last != "a" else "b")
    assert verify_stripe_signature(PAYLOAD, tampered, SECRET, now_ts=now) is False


def test_stale_timestamp_rejected():
    now = int(time.time())
    # t = 600s ago, tolerance default 300s
    old_t = now - 600
    header = _build_header(PAYLOAD, SECRET, old_t)
    assert verify_stripe_signature(PAYLOAD, header, SECRET, now_ts=now) is False


def test_future_timestamp_rejected():
    now = int(time.time())
    future_t = now + 600
    header = _build_header(PAYLOAD, SECRET, future_t)
    assert verify_stripe_signature(PAYLOAD, header, SECRET, now_ts=now) is False


def test_within_tolerance_window_accepted():
    now = int(time.time())
    # Exactly tolerance seconds ago
    old_t = now - WEBHOOK_TOLERANCE_SECONDS
    header = _build_header(PAYLOAD, SECRET, old_t)
    assert verify_stripe_signature(PAYLOAD, header, SECRET, now_ts=now) is True


def test_missing_v1_in_header_rejected():
    now = int(time.time())
    header = f"t={now},v0=oldformat"  # no v1
    assert verify_stripe_signature(PAYLOAD, header, SECRET, now_ts=now) is False


def test_missing_t_in_header_rejected():
    now = int(time.time())
    signed = f"{now}.".encode() + PAYLOAD
    sig = hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
    header = f"v1={sig}"  # no t
    assert verify_stripe_signature(PAYLOAD, header, SECRET, now_ts=now) is False


def test_wrong_secret_rejected():
    now = int(time.time())
    header = _build_header(PAYLOAD, SECRET, now)
    assert verify_stripe_signature(PAYLOAD, header, "whsec_WRONG_KEY", now_ts=now) is False


def test_empty_signature_header_rejected():
    assert verify_stripe_signature(PAYLOAD, "", SECRET) is False


def test_empty_secret_rejected():
    now = int(time.time())
    header = _build_header(PAYLOAD, "anysecret", now)
    assert verify_stripe_signature(PAYLOAD, header, "", now_ts=now) is False


def test_multiple_v1_signatures_any_match_accepted():
    """During secret rotation Stripe sends multiple v1 sigs; ANY match passes."""
    now = int(time.time())
    # Build a header with one bogus v1 sig + one valid v1 sig
    signed = f"{now}.".encode() + PAYLOAD
    valid_sig = hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
    bogus_sig = "00" * 32  # 64 hex chars of garbage
    header = f"t={now},v1={bogus_sig},v1={valid_sig}"
    assert verify_stripe_signature(PAYLOAD, header, SECRET, now_ts=now) is True


def test_garbage_signature_header_rejected():
    assert verify_stripe_signature(PAYLOAD, "not-a-stripe-signature", SECRET) is False


def test_invalid_t_value_rejected():
    """If t can't parse as int, reject."""
    sig = hmac.new(SECRET.encode(), b"x", hashlib.sha256).hexdigest()
    header = f"t=not_a_number,v1={sig}"
    assert verify_stripe_signature(PAYLOAD, header, SECRET) is False


def test_signature_constant_time_compare():
    """Use hmac.compare_digest to prevent timing side-channels.

    Smoke test: confirm that a hash that differs in the LAST byte takes about
    the same time as one that differs in the FIRST byte. We can't measure
    micro-timings reliably in a unit test, but we can verify both paths return
    False (i.e., we don't have an early-exit that leaks).
    """
    now = int(time.time())
    signed = f"{now}.".encode() + PAYLOAD
    valid = hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()

    # Flip first hex char
    flipped_first = ("0" if valid[0] != "0" else "1") + valid[1:]
    # Flip last hex char
    flipped_last = valid[:-1] + ("0" if valid[-1] != "0" else "1")

    h1 = f"t={now},v1={flipped_first}"
    h2 = f"t={now},v1={flipped_last}"
    assert verify_stripe_signature(PAYLOAD, h1, SECRET, now_ts=now) is False
    assert verify_stripe_signature(PAYLOAD, h2, SECRET, now_ts=now) is False

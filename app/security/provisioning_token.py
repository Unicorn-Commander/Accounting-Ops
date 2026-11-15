# Copyright (C) 2026 Magic Unicorn Unconventional Technology & Stuff Inc
# and the Accounting-Ops contributors.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in control-plane authentication; replay protection shared by all workers."""
import hashlib
import json
import logging
import os
import time
from functools import lru_cache

import jwt
from fastapi import HTTPException, Request
from sqlalchemy import text

from app.db import SessionLocal

REPLAY_CAPACITY = 10000


def provisioning_enabled() -> bool:
    return os.getenv("PROVISIONING_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def _warn_secret() -> None:
    logging.getLogger(__name__).warning("PROVISIONING_JWT_SECRET must contain at least 32 bytes")


def provisioning_available() -> bool:
    if not provisioning_enabled():
        return False
    if len(os.getenv("PROVISIONING_JWT_SECRET", "").encode()) < 32:
        _warn_secret()
        return False
    return True


def verify_claims(token: str, secret: str, audience: str, now: float) -> dict:
    """Verify the signature without PyJWT's independent wall-clock reading."""
    try:
        verified = jwt.api_jws.decode_complete(token, secret, algorithms=["HS256"])
        claims = json.loads(verified["payload"])
        if not isinstance(claims, dict):
            raise ValueError
        issued, expires = claims.get("iat"), claims.get("exp")
        if type(issued) is not int or type(expires) is not int:
            raise ValueError
        if issued > now + 5 or expires <= now or not 0 < expires - issued <= 60:
            raise ValueError
        if claims.get("iss") != "ops-center" or claims.get("aud") != audience:
            raise ValueError
        if not isinstance(claims.get("jti"), str) or not 1 <= len(claims["jti"]) <= 255:
            raise ValueError
        if "nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > now):
            raise ValueError
        return claims
    except (jwt.PyJWTError, ValueError, TypeError, UnicodeError) as exc:
        raise HTTPException(401, "Invalid provisioning token") from exc


async def consume_jti(jti: str, expires: int, now: float) -> None:
    # A single transaction + PG advisory lock makes capacity and replay checks
    # atomic across processes. Hashing prevents unbounded or sensitive payloads.
    digest = hashlib.sha256(jti.encode()).hexdigest()
    async with SessionLocal() as session, session.begin():
        await session.execute(text("SELECT pg_advisory_xact_lock(218, 1)"))
        await session.execute(
            text("DELETE FROM public.provisioning_replay WHERE expires_at <= :now"), {"now": now}
        )
        if (await session.execute(
            text("SELECT 1 FROM public.provisioning_replay WHERE digest = :digest"),
            {"digest": digest},
        )).first():
            raise HTTPException(401, "Replayed provisioning token")
        count = (await session.execute(
            text("SELECT count(*) FROM public.provisioning_replay")
        )).scalar_one()
        if count >= REPLAY_CAPACITY:
            raise HTTPException(503, "Provisioning replay store is full")
        await session.execute(
            text("INSERT INTO public.provisioning_replay (digest, expires_at) VALUES (:d, :e)"),
            {"d": digest, "e": expires},
        )


async def require_provisioning_token(request: Request) -> None:
    if not provisioning_available():
        raise HTTPException(404, "Not Found")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Provisioning bearer required")
    now = time.time()
    claims = verify_claims(
        token, os.environ["PROVISIONING_JWT_SECRET"],
        os.getenv("PROVISIONING_JWT_AUDIENCE", "accounting-ops"), now,
    )
    await consume_jti(claims["jti"], claims["exp"], now)

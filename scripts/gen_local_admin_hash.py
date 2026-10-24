#!/usr/bin/env python3
"""Generate an AO_LOCAL_ADMIN_PASSWORD_HASH for Community Edition self-host auth.

The web NextAuth Credentials provider (web/lib/auth.ts) verifies the admin
password against a scrypt hash held in AO_LOCAL_ADMIN_PASSWORD_HASH. This helper
produces that value in the expected ``scrypt:<saltHex>:<derivedKeyHex>`` format
using the SAME parameters Node's crypto.scryptSync uses by default (N=16384,
r=8, p=1), so a hash minted here validates in the web layer unchanged.

Usage:
    python scripts/gen_local_admin_hash.py            # prompts (no echo) on a TTY
    AO_LOCAL_ADMIN_PASSWORD=... python scripts/gen_local_admin_hash.py
                                                      # non-interactive (installers, CI)
    python scripts/gen_local_admin_hash.py 'password' # arg (shell history!)

Without a TTY and without AO_LOCAL_ADMIN_PASSWORD the helper refuses (exit 2)
instead of crashing in getpass; the bootstrap then generates a password itself.

Then set in your .env:
    AO_AUTH_MODE=local
    AO_EDITION=community
    AO_LOCAL_ADMIN_EMAIL=admin@example.com
    AO_LOCAL_ADMIN_UC_UID=admin
    AO_LOCAL_ADMIN_PASSWORD_HASH=scrypt:....:....
    AO_LOCAL_AUTH_SECRET=<a long random string, shared web<->backend>
"""
from __future__ import annotations

import getpass
import hashlib
import os
import sys

# Node crypto.scryptSync defaults (must match web/lib/auth.ts verify path).
_N = 16384
_R = 8
_P = 1
_KEYLEN = 64
_SALT_BYTES = 16
# Node's default maxmem is 32MB; scrypt with N=16384,r=8 needs ~16MB → bump the
# Python maxmem so hashlib.scrypt doesn't raise "memory limit exceeded".
_MAXMEM = 64 * 1024 * 1024


def main() -> int:
    env_password = os.environ.get("AO_LOCAL_ADMIN_PASSWORD")
    if len(sys.argv) > 1:
        password = sys.argv[1]
    elif env_password is not None:
        password = env_password
    elif not sys.stdin.isatty():
        print(
            "No TTY to prompt on: set AO_LOCAL_ADMIN_PASSWORD for a "
            "non-interactive install.",
            file=sys.stderr,
        )
        return 2
    else:
        password = getpass.getpass("Admin password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            return 1
    if not password:
        print("Empty password refused.", file=sys.stderr)
        return 1

    salt = os.urandom(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_N,
        r=_R,
        p=_P,
        dklen=_KEYLEN,
        maxmem=_MAXMEM,
    )
    print(f"scrypt:{salt.hex()}:{derived.hex()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

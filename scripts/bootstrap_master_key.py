"""Generate the Accounting-Ops master encryption key. Run ONCE per deployment.

The key is 32 random bytes, written to the configured secret path with
mode 0600. Refuses to overwrite an existing key (orphan-ciphertext footgun).

Usage:
    python -m scripts.bootstrap_master_key
    python -m scripts.bootstrap_master_key --path /run/secrets/accounting_ops_master_key

Persistence:
- On midboy1 deploy, mount the directory containing this file as a Docker
  secret or named volume and chmod 600.
- Back up the file out-of-band (encrypted) immediately. Losing this key
  permanently orphans every encrypted credential in the database.
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        default="/run/secrets/accounting_ops_master_key",
        help="Where to write the 32-byte master key (default: /run/secrets/accounting_ops_master_key)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing key (DANGEROUS — orphans existing ciphertext)",
    )
    args = parser.parse_args()

    path = Path(args.path)
    if path.exists() and not args.force:
        print(
            f"ERROR: {path} already exists. Refusing to overwrite "
            "(would orphan existing ciphertext). Pass --force if you "
            "have a backup and know what you're doing.",
            file=sys.stderr,
        )
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(secrets.token_bytes(32))
    path.chmod(0o600)
    print(f"wrote 32-byte master key to {path} (chmod 600)")
    print(
        "BACK UP THIS FILE NOW. Losing it permanently orphans all "
        "encrypted credentials in the database."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

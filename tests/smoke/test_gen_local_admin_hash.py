"""Non-interactive local-admin password paths for the self-host installer."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/gen_local_admin_hash.py"


def _run(env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "AO_LOCAL_ADMIN_PASSWORD"}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_env_password_yields_node_compatible_scrypt_hash():
    result = _run({"AO_LOCAL_ADMIN_PASSWORD": "cleanroom-example-pw"})
    assert result.returncode == 0, result.stderr
    tag, salt_hex, key_hex = result.stdout.strip().split(":")
    assert tag == "scrypt"
    derived = hashlib.scrypt(
        b"cleanroom-example-pw",
        salt=bytes.fromhex(salt_hex),
        n=16384,
        r=8,
        p=1,
        dklen=64,
        maxmem=64 * 1024 * 1024,
    )
    assert derived.hex() == key_hex
    assert "cleanroom-example-pw" not in result.stdout + result.stderr


def test_no_tty_and_no_env_refuses_cleanly_instead_of_crashing():
    result = _run({})
    assert result.returncode == 2
    assert "AO_LOCAL_ADMIN_PASSWORD" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


def test_empty_env_password_is_refused():
    result = _run({"AO_LOCAL_ADMIN_PASSWORD": ""})
    assert result.returncode == 1
    assert result.stdout == ""


def test_bootstrap_never_stores_the_plaintext_admin_password():
    source = (REPO_ROOT / "scripts/selfhost_bootstrap.sh").read_text()
    assert 'set_env AO_LOCAL_ADMIN_PASSWORD "' not in source
    assert "set_env AO_LOCAL_ADMIN_PASSWORD_HASH" in source
    # Non-interactive branches exist: env var, TTY prompt, generated fallback.
    assert '"${AO_LOCAL_ADMIN_PASSWORD:-}"' in source
    assert "[ -t 0 ]" in source
    assert "unset _generated_admin_password" in source

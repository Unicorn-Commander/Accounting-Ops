"""``download_irs_forms.sh --refresh <form>`` replaces one cached PDF.

The fake ``curl`` never touches the network. ``IRS_FORMS_DOWNLOAD_DIR`` keeps
the run out of ``data/irs_forms``. Without the flag, a file that is already
present is still skipped.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "download_irs_forms.sh"

_FAKE_CURL = """#!/bin/bash
set -euo pipefail
args=("$@")
for arg in "${args[@]}"; do
    if [[ "$arg" == "-fsI" || "$arg" == "-I" || "$arg" == "--head" ]]; then
        exit 0
    fi
done
out=""
prev=""
for arg in "${args[@]}"; do
    if [[ "$prev" == "-o" ]]; then
        out="$arg"
    fi
    prev="$arg"
done
if [[ -z "$out" ]]; then
    exit 0
fi
url="${args[${#args[@]}-1]}"
printf 'downloaded %s\\n' "$url" > "$out"
"""


def _env(tmp: Path, fake_bin: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["IRS_FORMS_DOWNLOAD_DIR"] = str(tmp)
    env.pop("PYTHONPATH", None)
    return env


def _install_curl(fake_bin: Path) -> None:
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(_FAKE_CURL)
    curl.chmod(0o755)


def _run(tmp: Path, fake_bin: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        cwd=str(_REPO),
        env=_env(tmp, fake_bin),
        text=True,
        capture_output=True,
        check=False,
    )


def test_without_refresh_an_existing_file_is_skipped(tmp_path: Path):
    forms = tmp_path / "forms"
    forms.mkdir()
    fake_bin = tmp_path / "bin"
    _install_curl(fake_bin)
    original = b"already-here\n"
    (forms / "f9465.pdf").write_bytes(original)
    (forms / "f1040.pdf").write_bytes(original)

    result = _run(forms, fake_bin, [])

    assert result.returncode == 0, result.stderr
    assert (forms / "f9465.pdf").read_bytes() == original
    assert (forms / "f1040.pdf").read_bytes() == original
    assert "SKIP" in result.stdout
    assert "OLD sha256" not in result.stdout
    assert "NEW sha256" not in result.stdout
    assert not ( _REPO / "data" / "irs_forms" / "f9465.pdf" ).exists()


def test_refresh_prints_old_and_new_sha_and_leaves_other_files(tmp_path: Path):
    forms = tmp_path / "forms"
    forms.mkdir()
    fake_bin = tmp_path / "bin"
    _install_curl(fake_bin)
    original = b"rev-9-2020-bytes\n"
    kept = b"leave-this-1040\n"
    (forms / "f9465.pdf").write_bytes(original)
    (forms / "f1040.pdf").write_bytes(kept)
    old_sha = hashlib.sha256(original).hexdigest()

    result = _run(forms, fake_bin, ["--refresh", "f9465"])

    assert result.returncode == 0, result.stderr
    new_bytes = (forms / "f9465.pdf").read_bytes()
    assert new_bytes != original
    assert b"f9465.pdf" in new_bytes
    new_sha = hashlib.sha256(new_bytes).hexdigest()
    assert f"OLD sha256 {old_sha}" in result.stdout
    assert f"NEW sha256 {new_sha}" in result.stdout
    assert (forms / "f1040.pdf").read_bytes() == kept
    assert "SKIP" in result.stdout


def test_refresh_requires_a_form_name(tmp_path: Path):
    forms = tmp_path / "forms"
    forms.mkdir()
    fake_bin = tmp_path / "bin"
    _install_curl(fake_bin)

    result = _run(forms, fake_bin, ["--refresh"])

    assert result.returncode == 2
    assert "requires a form name" in result.stderr
    assert list(forms.iterdir()) == []


def test_refresh_of_an_unknown_form_fails(tmp_path: Path):
    forms = tmp_path / "forms"
    forms.mkdir()
    fake_bin = tmp_path / "bin"
    _install_curl(fake_bin)

    result = _run(forms, fake_bin, ["--refresh", "f9999"])

    assert result.returncode == 1
    assert "did not match" in result.stderr
    assert "OLD sha256" not in result.stdout

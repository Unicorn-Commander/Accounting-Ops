"""Guard: every curated primary field key is a REAL renderer input key.

A wrong key in ``form_field_specs`` would silently not render (the renderers skip
unmapped keys) — the worst kind of bug because nothing errors. This test asserts
each primary key is recognized by its renderer, either as a field-map dict key or
as a special-branch literal in the fill/letter module source.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app.exports.form_field_specs import FORM_PRIMARY_FIELDS

_EXPORTS = Path(__file__).resolve().parents[2] / "app" / "exports"

# kind -> (field_map module | None, fill/letter source filename)
_RENDERERS: dict[str, tuple[str | None, str]] = {
    "sch-a": ("sch_a_field_map", "sch_a_fill.py"),
    "sch-b": ("sch_b_field_map", "sch_b_fill.py"),
    "sch-c": ("sch_c_field_map", "sch_c_fill.py"),
    "sch-d": ("sch_d_field_map", "sch_d_fill.py"),
    "sch-e": ("sch_e_field_map", "sch_e_fill.py"),
    "sch-se": ("sch_se_field_map", "sch_se_fill.py"),
    "1065": ("f1065_field_map", "f1065_fill.py"),
    "1120-s": ("f1120s_field_map", "f1120s_fill.py"),
    "k1-1065": ("k1_1065_field_map", "k1_1065_fill.py"),
    "941": ("f941_field_map", "f941_fill.py"),
    "4868": ("f4868_field_map", "f4868_fill.py"),
    "ca-540": ("ca_540_field_map", "ca_540_fill.py"),
    "st-3": (None, "st3_fill.py"),
    "cp504": (None, "cp504_letter.py"),
    "1120": ("form_1120_field_map", "form_1120_fill.py"),
    "sc1120": (None, "sc_form_prefill.py"),
    "scdor-notice": (None, "scdor_letter.py"),
}


def _field_map_keys(modname: str) -> set[str]:
    mod = importlib.import_module(f"app.exports.{modname}")
    keys: set[str] = set()
    for name, val in vars(mod).items():
        if name.startswith("_") or not isinstance(val, dict):
            continue
        keys.update(k for k in val if isinstance(k, str))
    return keys


def _valid_keys(kind: str) -> tuple[set[str], str]:
    """Return (field-map dict keys, fill-module source) for a kind. Special-branch
    keys (e.g. f941 "name"/"quarter", sch-c "accounting_method", cp504 letter
    inputs) aren't dict keys, so the test also scans ``src`` for the literal."""
    mapmod, fill_src = _RENDERERS[kind]
    valid: set[str] = _field_map_keys(mapmod) if mapmod else set()
    src = (_EXPORTS / fill_src).read_text()
    return valid, src


def test_every_kind_has_a_renderer_mapping() -> None:
    missing = [k for k in FORM_PRIMARY_FIELDS if k not in _RENDERERS]
    assert not missing, f"form_field_specs kinds with no renderer mapping: {missing}"


@pytest.mark.parametrize("kind", sorted(FORM_PRIMARY_FIELDS))
def test_primary_keys_are_real_renderer_keys(kind: str) -> None:
    valid, src = _valid_keys(kind)
    bad = []
    for field in FORM_PRIMARY_FIELDS[kind]:
        key = field["key"]
        if key in valid:
            continue
        # Fall back to a source literal check (special-branch / letter inputs).
        if f'"{key}"' in src or f"'{key}'" in src:
            continue
        bad.append(key)
    assert not bad, f"{kind}: primary keys not recognized by the renderer: {bad}"


@pytest.mark.parametrize("kind", sorted(FORM_PRIMARY_FIELDS))
def test_field_shapes_are_well_formed(kind: str) -> None:
    allowed_types = {
        "currency", "integer", "text", "checkbox", "date", "ssn", "ein", "percent",
    }
    for field in FORM_PRIMARY_FIELDS[kind]:
        assert field.get("key"), f"{kind}: a field is missing 'key'"
        assert field.get("label"), f"{kind}: {field['key']} missing 'label'"
        assert field.get("type") in allowed_types, (
            f"{kind}: {field['key']} has bad type {field.get('type')!r}"
        )

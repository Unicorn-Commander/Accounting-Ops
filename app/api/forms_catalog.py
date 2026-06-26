"""Forms catalog API — lets the UI enumerate every form the platform can produce.

The form registry (``app/exports/form_registry.py``) is the source of truth for
*which* forms exist; this read-only, auth-only (no workspace scope) endpoint
surfaces it to the browser so the Forms hub can render the catalog and a prepare
screen can ask for a form's primary input fields.

  GET /api/forms
      List every registered form: kind, title, jurisdiction, mode, who it goes
      to, category. The catalog the Forms hub renders.

  GET /api/forms/{kind}
      One form's spec PLUS its ``fields`` — the curated primary input fields a
      preparer fills for a ``fill``/``letter`` form (empty for ``computed`` forms,
      which have bespoke guided UIs). Drives the generic prepare form.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth import AuthContext, validate_bearer_token
from app.exports.form_field_specs import primary_fields_for
from app.exports.form_registry import get_spec, list_forms

router = APIRouter(tags=["forms"])


def _spec_dict(spec) -> dict:
    return {
        "kind": spec.kind,
        "title": spec.title,
        "jurisdiction": spec.jurisdiction,
        "mode": spec.mode,
        "goes_to": spec.goes_to,
        "category": spec.category,
    }


@router.get("/api/forms")
async def list_available_forms(
    _auth: AuthContext = Depends(validate_bearer_token),
) -> dict:
    """List every registered form (the platform-wide catalog)."""
    return {"items": [_spec_dict(s) for s in list_forms()]}


@router.get("/api/forms/{kind}")
async def get_form_spec(
    kind: str,
    _auth: AuthContext = Depends(validate_bearer_token),
) -> dict:
    """One form's spec + its curated primary input fields (for the prepare UI)."""
    spec = get_spec(kind)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"No form registered for kind {kind!r}.")
    out = _spec_dict(spec)
    out["fields"] = primary_fields_for(spec.kind)
    return out

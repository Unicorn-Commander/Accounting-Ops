"""Smoke tests for the self-serve onboarding router (no live stack required).

Validates the request contract + route registration. The end-to-end provision
path (zero-membership user -> new schema + owner membership) is covered by
tests/integration/test_onboarding_provision.py against a live stack.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.onboarding import ProvisionFirstWorkspaceRequest, router


def test_route_is_registered() -> None:
    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/api/onboarding/provision-first-workspace" in paths


def test_display_name_is_required() -> None:
    with pytest.raises(ValidationError):
        ProvisionFirstWorkspaceRequest(display_name="")


def test_display_name_max_length_enforced() -> None:
    with pytest.raises(ValidationError):
        ProvisionFirstWorkspaceRequest(display_name="x" * 121)


def test_defaults_are_safe() -> None:
    req = ProvisionFirstWorkspaceRequest(display_name="Acme LLC")
    assert req.isolation_tier == "shared"
    assert req.hipaa_protected is False

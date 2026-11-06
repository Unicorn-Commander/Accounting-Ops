"""Read Accounting-Ops access from the Ops-Center entitlements endpoint.

The endpoint accepts the caller's uchub bearer and is the source of truth for
which Suite applications the caller's organization purchased.  This module is
deliberately small and read-only: Accounting-Ops never mutates billing state.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.config import get_settings

_log = logging.getLogger("acctops.security.entitlements")

ENTITLEMENTS_PATH = "/api/v1/entitlements"
ACCOUNTING_OPS_FEATURE = "accounting_ops_access"
ENTITLEMENTS_TIMEOUT_SECONDS = 5.0
_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class EntitlementResult:
    """A safe, normalized result from Ops-Center."""

    status: Literal["entitled", "not_entitled", "unavailable"]
    org_id: str | None = None
    org_name: str | None = None
    org_role: str | None = None
    reason: str = ""


def entitled_provisioning_enabled() -> bool:
    """Return whether the opt-in first-call provisioning flag is enabled."""
    return os.getenv("AO_ENTITLED_PROVISIONING", "").strip().lower() in _TRUTHY


def bearer_from_header(value: str | None) -> str | None:
    """Extract a bearer token without accepting another authorization scheme."""
    if not value:
        return None
    parts = value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


class OpsCenterEntitlementsClient:
    """Small HTTP client for the caller-scoped Ops-Center entitlement lookup."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        configured = base_url
        if configured is None:
            configured = get_settings().ops_center_base_url
        self.base_url = (configured or "").strip().rstrip("/")
        self._http_client = http_client

    async def fetch(self, bearer: str | None) -> EntitlementResult:
        """Fetch and normalize entitlements; transport failures never raise."""
        if not self.base_url:
            return EntitlementResult("unavailable", reason="Ops-Center base URL is unset")
        if not bearer or not bearer.strip():
            return EntitlementResult("unavailable", reason="caller bearer is missing")

        url = f"{self.base_url}{ENTITLEMENTS_PATH}"
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
        }
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    url, headers=headers, timeout=ENTITLEMENTS_TIMEOUT_SECONDS
                )
            else:
                async with httpx.AsyncClient(timeout=ENTITLEMENTS_TIMEOUT_SECONDS) as client:
                    response = await client.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001 - fail closed, never leak auth material
            _log.warning(
                "Ops-Center entitlements request failed (%s)", exc.__class__.__name__
            )
            return EntitlementResult("unavailable", reason="Ops-Center is unreachable")

        if response.status_code in (401, 403):
            return EntitlementResult(
                "not_entitled", reason="Ops-Center rejected the caller bearer"
            )
        if response.status_code != 200:
            _log.warning("Ops-Center entitlements returned HTTP %s", response.status_code)
            return EntitlementResult(
                "unavailable", reason="Ops-Center returned no entitlement verdict"
            )

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - malformed response is fail closed
            _log.warning(
                "Ops-Center entitlements response was not JSON (%s)",
                exc.__class__.__name__,
            )
            return EntitlementResult(
                "unavailable", reason="Ops-Center returned an invalid response"
            )

        if not isinstance(body, dict):
            return EntitlementResult(
                "unavailable", reason="Ops-Center returned an invalid response"
            )

        if not _feature_enabled(body, ACCOUNTING_OPS_FEATURE):
            return EntitlementResult(
                "not_entitled", reason="organization lacks Accounting-Ops access"
            )

        org_id = _first_string(
            body.get("org_id"),
            body.get("organization_id"),
            _nested(body, "data", "org_id"),
            _nested(body, "data", "organization_id"),
            _nested(body, "organization", "id"),
        )
        if not org_id:
            return EntitlementResult(
                "unavailable", reason="entitlement is missing an organization id"
            )

        org_name = _first_string(
            body.get("org_name"),
            _nested(body, "data", "org_name"),
        )
        org_role = _first_string(
            body.get("org_role"),
            body.get("role"),
            _nested(body, "data", "org_role"),
            _nested(body, "data", "role"),
            _nested(body, "organization", "role"),
        )
        return EntitlementResult(
            "entitled",
            org_id=org_id,
            org_name=org_name,
            org_role=org_role,
            reason="organization carries accounting_ops_access",
        )


def _nested(body: dict[str, Any], parent: str, child: str) -> Any:
    value = body.get(parent)
    return value.get(child) if isinstance(value, dict) else None


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _feature_enabled(body: dict[str, Any], feature: str) -> bool:
    """Accept the measured list/map shapes without treating unknown data as access."""
    containers: list[Any] = [body]
    data = body.get("data")
    if isinstance(data, dict):
        containers.append(data)

    for container in containers:
        for field in ("entitlements", "feature_keys", "featureKeys", "features"):
            if _feature_in_value(container.get(field), feature):
                return True
        for field in ("tier_features", "org_features"):
            if _feature_in_value(container.get(field), feature):
                return True
    return False


def _feature_in_value(value: Any, feature: str) -> bool:
    if isinstance(value, dict):
        enabled = value.get(feature)
        if enabled is True:
            return True
        if isinstance(enabled, dict) and enabled.get("enabled") is True:
            return True
        return False
    if not isinstance(value, list):
        return False
    for item in value:
        if item == feature:
            return True
        if isinstance(item, dict):
            key = item.get("key") or item.get("name") or item.get("feature")
            if key == feature and item.get("enabled", True) is True:
                return True
    return False


__all__ = (
    "ACCOUNTING_OPS_FEATURE",
    "ENTITLEMENTS_PATH",
    "EntitlementResult",
    "OpsCenterEntitlementsClient",
    "bearer_from_header",
    "entitled_provisioning_enabled",
)

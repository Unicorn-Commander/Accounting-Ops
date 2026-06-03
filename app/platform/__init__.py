"""UC platform-integration helpers (org resolution, entitlement, billing reads).

This package is how Accounting-Ops CONSUMES the Unicorn Commander platform
(Ops-Center + Lago + Keycloak): resolve a workspace's org, read its entitlement,
and surface usage. AO never owns billing or mutates Lago/Stripe — it integrates.
"""

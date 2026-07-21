"""Hosted-tier billing/entitlement enforcement for Accounting-Ops.

This package holds the ENTERPRISE-HOLD credit/entitlement gate for the hosted
product (paid, metered inference). It is inert unless explicitly enabled
(``AO_SMART_GATE_ENABLED=1``) and is always exempt in Community Edition
(``AO_EDITION=community``). The Community self-host build excludes this package
from its snapshot (see COMMUNITY_MANIFEST.md → ENTERPRISE-HOLD).

Design principle: AO never duplicates the wallet. Credits + coupons live in
Ops-Center; per-call spend is enforced at the LiteLLM gateway (per-workspace
virtual key ``max_budget`` + Lago metering). This package only decides
*access* — may this workspace invoke a paid/cloud "smart" function at all —
and reads entitlement from the caller's token claim (the same seam
``app/ar/entitlement.py`` already exposes).
"""

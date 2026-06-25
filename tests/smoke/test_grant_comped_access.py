"""Smoke tests for scripts/grant_comped_access.py (operator comp tool).

Pure-python: no DB / no network. Guards the two things that silently break a
comp if they drift — the argparse surface and the resolver storage contract
(the script MUST store credentials under the exact connector/label the resolver
in app/security/workspace_llm_keys.py reads back).
"""
from __future__ import annotations

import json

import pytest

import scripts.grant_comped_access as gca
from app.security import workspace_llm_keys as wlk


def test_last4_masks_keys():
    assert gca._last4("sk-ant-abcd1234") == "1234"
    assert gca._last4("xyz") == "xyz"  # shorter than 4 → returned whole (still tiny)
    assert gca._last4("") == ""


def test_storage_constants_lockstep_with_resolver():
    # If these drift, a comped key is written where the resolver never looks.
    assert gca.CONNECTOR_BYOK == wlk.CONNECTOR_BYOK
    assert gca.CONNECTOR_VIRTUAL == wlk.CONNECTOR_VIRTUAL
    assert gca._VIRTUAL_LABEL == wlk._VIRTUAL_LABEL == "platform"


def test_byok_json_shape_matches_get_byok():
    # The shape store_byok persists must be exactly what get_byok parses.
    value = json.dumps(
        {"key": "sk-ant-xyz", "provider": "anthropic", "base_url": None}
    )
    blob = json.loads(value)
    assert blob["key"] == "sk-ant-xyz"
    assert blob["provider"] == "anthropic"
    assert "base_url" in blob


def test_parser_byok_requires_provider_and_key():
    parser = gca._build_parser()
    args = parser.parse_args(
        ["byok", "--workspace", "w", "--provider", "anthropic", "--key", "k"]
    )
    assert args.mode == "byok"
    assert args.provider == "anthropic"
    with pytest.raises(SystemExit):
        parser.parse_args(["byok", "--workspace", "w", "--provider", "anthropic"])
    # provider is constrained to the allowed set
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["byok", "--workspace", "w", "--provider", "cohere", "--key", "k"]
        )


def test_parser_credit_and_org():
    parser = gca._build_parser()
    credit = parser.parse_args(
        ["credit", "--workspace", "w", "--budget", "25", "--uc-org-id", "org_1"]
    )
    assert credit.mode == "credit"
    assert credit.budget == 25.0
    assert credit.uc_org_id == "org_1"

    org = parser.parse_args(["org", "--workspace", "w", "--uc-org-id", "org_2"])
    assert org.mode == "org"
    assert org.uc_org_id == "org_2"
    # org mode requires --uc-org-id
    with pytest.raises(SystemExit):
        parser.parse_args(["org", "--workspace", "w"])


def test_parser_requires_a_mode():
    parser = gca._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])

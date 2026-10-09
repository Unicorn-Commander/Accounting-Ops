"""Unit tests for the specialist/narrative output-token configuration."""
from __future__ import annotations

import pytest

from agents._shared.limits import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    get_max_output_tokens,
    parse_max_output_tokens,
)


def test_default_is_the_flat_large_budget():
    assert get_max_output_tokens({}) == DEFAULT_MAX_OUTPUT_TOKENS == 48_000


def test_preferred_environment_variable_wins_over_legacy_name():
    assert get_max_output_tokens(
        {"AO_MAX_OUTPUT_TOKENS": "64000", "MAX_OUTPUT_TOKENS": "32000"}
    ) == 64_000


def test_legacy_environment_variable_is_supported():
    assert get_max_output_tokens({"MAX_OUTPUT_TOKENS": "32000"}) == 32_000


def test_environment_override_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("AO_MAX_OUTPUT_TOKENS", "50000")
    assert get_max_output_tokens() == 50_000
    monkeypatch.setenv("AO_MAX_OUTPUT_TOKENS", "60000")
    assert get_max_output_tokens() == 60_000


@pytest.mark.parametrize("raw", ["0", "-1", "131073", "not-an-int", "1.5", ""])
def test_invalid_limits_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_max_output_tokens(raw)


@pytest.mark.parametrize("raw", ["1", " 48000 ", "131072"])
def test_valid_limits_are_accepted(raw):
    assert 1 <= parse_max_output_tokens(raw) <= 131_072

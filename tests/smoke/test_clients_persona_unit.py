"""Pure unit tests for the CPA console's seating + persona rules — no DB / no network.

Why these exist: a practitioner who onboards a client through
``POST /api/me/clients`` must be able to run that workspace end to end (bank
connect = admin floor, invitations incl. elevated seats = owner floor), and the
landing router must still recognise them as a practitioner even though the
seat they hold there is ``owner``, not ``cpa``.
"""
from __future__ import annotations

import pytest

from app.api.clients import (
    _PRACTITIONER_ROLES,
    CLIENT_OWNER_INVITE_ROLE,
    CLIENT_SEAT_ROLE,
    _persona_for,
)
from app.api.members import ELEVATED_ROLES, INVITEABLE_ROLES
from app.tenancy import _ROLE_RANK, role_at_least

# ---------------------------------------------------------------------------
# Seating: the console seat clears every floor a practice needs in a client
# workspace, and the client's seat is owner-rank and inviteable.
# ---------------------------------------------------------------------------


def test_console_seat_is_owner():
    assert CLIENT_SEAT_ROLE == "owner"


@pytest.mark.parametrize(
    "floor",
    [
        "accountant",  # setup bootstrap / create_entity_with_coa
        "admin",  # credentials/simplefin/claim, invitations, period close, decide_approval (MCP)
        "owner",  # elevated invites (admin/owner/client_owner), manual_link invites, data rights
    ],
)
def test_console_seat_clears_every_practice_floor(floor: str):
    assert role_at_least(CLIENT_SEAT_ROLE, floor)


def test_client_owner_invite_is_owner_rank_and_inviteable():
    assert CLIENT_OWNER_INVITE_ROLE == "client_owner"
    assert _ROLE_RANK[CLIENT_OWNER_INVITE_ROLE] == _ROLE_RANK["owner"]
    assert CLIENT_OWNER_INVITE_ROLE in INVITEABLE_ROLES
    # Elevated: only an owner may grant it through the members API — which the
    # console seat is.
    assert CLIENT_OWNER_INVITE_ROLE in ELEVATED_ROLES
    assert role_at_least(CLIENT_SEAT_ROLE, "owner")


def test_owner_and_cpa_can_add_clients():
    # Morgan's case: owner of his own practice workspace, no cpa seat anywhere.
    assert "owner" in _PRACTITIONER_ROLES
    assert "cpa" in _PRACTITIONER_ROLES
    assert "bookkeeper" not in _PRACTITIONER_ROLES
    assert "viewer" not in _PRACTITIONER_ROLES


# ---------------------------------------------------------------------------
# Persona: platform_admin > cpa > client > new; onboarding a client makes a
# practitioner even when every seat held is owner.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "roles,onboarded,expected",
    [
        ([], 0, "new"),
        (["owner"], 0, "client"),  # a customer who owns their own books
        (["owner", "owner"], 0, "client"),  # two of their own businesses: still a customer
        (["viewer"], 0, "client"),
        (["cpa"], 0, "cpa"),  # outside CPA invited into someone's books
        (["owner", "cpa"], 0, "cpa"),
        (["owner"], 1, "cpa"),  # practice owner who onboarded one client (owner seat)
        (["owner", "owner"], 3, "cpa"),
        ([], 1, "cpa"),  # onboarded a client, no active seat left (client removed them)
        (["owner", "platform_admin"], 0, "platform_admin"),
        (["cpa", "platform_admin"], 5, "platform_admin"),
    ],
)
def test_persona_matrix(roles: list[str], onboarded: int, expected: str):
    assert _persona_for(roles, onboarded) == expected


def test_persona_empty_label_for_list_endpoints():
    # /api/me/clients and /triage never said "new"; they keep answering "client".
    assert _persona_for([], 0, empty="client") == "client"
    assert _persona_for([], 0) == "new"
    assert _persona_for(iter(["owner"]), 1, empty="client") == "cpa"

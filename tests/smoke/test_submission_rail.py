"""Smoke tests for the DORMANT, default-OFF submission rail.

Two layers, no live stack (no Postgres / Keycloak / HTTP-to-a-real-app):

  A. Unit tests of app/submission/rail.py against a directly-constructed
     EngagementPolicyData (a frozen dataclass) — no DB. Assert the dormant
     invariants:
       * paper_print ALWAYS resolves + propose works + execute references the
         work-product's document.pdf and transmits nothing;
       * every transmission channel raises FeatureDisabled by default (no policy
         enable);
       * even when a channel is marked enabled in the policy, execute STILL raises
         FeatureDisabled (the dormant scaffolding invariant — structure exists,
         nothing transmits);
       * the irreversibility gate is an independent block (False for every
         transmission channel today).

  B. A FastAPI route-shape test of GET /submission/channels: mount the submission
     router on a minimal app, override resolve_workspace, and patch
     get_or_create_policy + workspace_session so the handler runs end-to-end
     against an in-memory fake policy. Assert all four transmission channels are
     disabled by default and paper_print is enabled.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.submission as api_mod
from app.engagement.policy import RAIL_CHANNELS, EngagementPolicyData
from app.models.ledger import ReviewMode
from app.submission.rail import (
    ALL_CHANNELS,
    EftpsAdapter,
    EfileIrsAdapter,
    EsignAdapter,
    FeatureDisabled,
    PaperPrintAdapter,
    StatePortalAdapter,
    SubmissionChannel,
    TRANSMISSION_CHANNELS,
    irreversibility_ok,
    resolve_adapter,
)
from app.tenancy import WorkspaceContext, resolve_workspace

_WORKSPACE_ID = uuid.uuid4()
_ENTITY_ID = uuid.uuid4()
_WP_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _policy(*, channels: dict | None = None, kill_switch: bool = False) -> EngagementPolicyData:
    """Construct an EngagementPolicyData directly (frozen dataclass — no DB)."""
    return EngagementPolicyData(
        workspace_id=_WORKSPACE_ID,
        review_mode=ReviewMode.PRO_REQUIRED,
        has_licensed_pro=False,
        prefer_local_inference=False,
        submission_channels=channels or {},
        rail_kill_switch=kill_switch,
    )


def _work_product() -> dict:
    return {
        "id": str(_WP_ID),
        "entity_id": str(_ENTITY_ID),
        "kind": "w-4p",
        "tax_year": 2026,
        "title": "W-4P",
        "status": "draft",
    }


# ---------------------------------------------------------------------------
# A. Rail invariants — constants line up with the policy module
# ---------------------------------------------------------------------------


class TestRailConstants:
    def test_transmission_channels_match_policy_rail_channels(self):
        assert TRANSMISSION_CHANNELS == tuple(RAIL_CHANNELS)

    def test_paper_print_is_not_a_transmission_channel(self):
        assert SubmissionChannel.PAPER_PRINT.value == "paper_print"
        assert SubmissionChannel.PAPER_PRINT.value not in TRANSMISSION_CHANNELS

    def test_all_channels_is_paper_plus_transmission(self):
        assert ALL_CHANNELS[0] == SubmissionChannel.PAPER_PRINT.value
        assert set(ALL_CHANNELS) == {SubmissionChannel.PAPER_PRINT.value, *TRANSMISSION_CHANNELS}


# ---------------------------------------------------------------------------
# A. PaperPrint — the always-on default
# ---------------------------------------------------------------------------


class TestPaperPrintDefault:
    def test_paper_print_resolves_regardless_of_policy(self):
        # Empty policy, kill-switch on — paper still resolves (not a transmission).
        for policy in (_policy(), _policy(kill_switch=True), None):
            adapter = resolve_adapter("paper_print", policy)
            assert isinstance(adapter, PaperPrintAdapter)

    def test_paper_print_preflight_ready(self):
        adapter = resolve_adapter("paper_print", _policy())
        pf = adapter.preflight(_work_product())
        assert pf["ready"] is True
        assert pf["enabled"] is True
        assert pf["transmits"] is False

    def test_paper_print_propose_describes_print_and_mail(self):
        adapter = resolve_adapter("paper_print", _policy())
        proposal = adapter.propose(_work_product())
        assert proposal["channel"] == "paper_print"
        assert proposal["transmits"] is False
        assert "mail" in proposal["description"].lower()

    def test_paper_print_execute_references_document_pdf_no_transmit(self):
        adapter = resolve_adapter("paper_print", _policy())
        ack = adapter.execute(_work_product(), idempotency_key="k-1")
        assert ack["transmitted"] is False
        assert ack["machine_confirmed"] is True
        # The print instruction references the work-product's document.pdf path.
        assert "document.pdf" in ack["document_pdf_path"]
        assert str(_WP_ID) in ack["document_pdf_path"]
        assert "document.pdf" in ack["detail"]


# ---------------------------------------------------------------------------
# A. Transmission channels are DISABLED by default
# ---------------------------------------------------------------------------


class TestTransmissionDisabledByDefault:
    @pytest.mark.parametrize("channel", TRANSMISSION_CHANNELS)
    def test_resolve_raises_feature_disabled_by_default(self, channel):
        # No policy enable → resolve refuses.
        with pytest.raises(FeatureDisabled) as exc:
            resolve_adapter(channel, _policy())
        assert exc.value.channel == channel

    @pytest.mark.parametrize("channel", TRANSMISSION_CHANNELS)
    def test_resolve_raises_when_policy_is_none(self, channel):
        with pytest.raises(FeatureDisabled):
            resolve_adapter(channel, None)

    def test_unknown_channel_raises_feature_disabled(self):
        with pytest.raises(FeatureDisabled):
            resolve_adapter("carrier_pigeon", _policy())

    @pytest.mark.parametrize("channel", TRANSMISSION_CHANNELS)
    def test_kill_switch_blocks_even_an_enabled_channel(self, channel):
        # Channel is enabled in the dict, but the global kill-switch is on.
        policy = _policy(channels={channel: True}, kill_switch=True)
        with pytest.raises(FeatureDisabled):
            resolve_adapter(channel, policy)


# ---------------------------------------------------------------------------
# A. The dormant-scaffolding invariant: even gated-on, nothing transmits
# ---------------------------------------------------------------------------


class TestDormantScaffoldingInvariant:
    def test_irreversibility_gate_blocks_every_transmission_channel(self):
        # Independent of policy — the irreversibility threshold is False today.
        for channel in TRANSMISSION_CHANNELS:
            assert irreversibility_ok(channel) is False
        # Paper is not a transmission and is always OK.
        assert irreversibility_ok("paper_print") is True

    @pytest.mark.parametrize("channel", TRANSMISSION_CHANNELS)
    def test_policy_enabled_still_blocked_by_irreversibility_at_resolve(self, channel):
        # Channel explicitly enabled + kill-switch off, BUT the independent
        # irreversibility gate still blocks resolve.
        policy = _policy(channels={channel: True})
        assert policy.channel_enabled(channel) is True  # policy gate passes
        with pytest.raises(FeatureDisabled):
            resolve_adapter(channel, policy)

    @pytest.mark.parametrize(
        "adapter_cls,channel",
        [
            (EfileIrsAdapter, SubmissionChannel.EFILE_IRS.value),
            (StatePortalAdapter, SubmissionChannel.STATE_PORTAL.value),
            (EftpsAdapter, SubmissionChannel.EFTPS.value),
            (EsignAdapter, SubmissionChannel.ESIGN.value),
        ],
    )
    def test_execute_raises_even_when_constructed_with_enabled_policy(self, adapter_cls, channel):
        """The core dormant invariant: construct the adapter directly against a
        policy that ENABLES the channel and call execute() with a valid
        idempotency key — it STILL raises FeatureDisabled for that channel.
        Structure exists; nothing transmits. (Blocked here by the independent
        irreversibility gate; the terminal 'not yet legally enabled' guard is
        covered by test_execute_terminal_guard_blocks_when_irreversibility_passes.)"""
        policy = _policy(channels={channel: True})
        adapter = adapter_cls(policy)
        with pytest.raises(FeatureDisabled) as exc:
            adapter.execute(_work_product(), idempotency_key="idem-123")
        # It's blocked, and not because the key was missing.
        assert exc.value.channel == channel

    @pytest.mark.parametrize(
        "adapter_cls,channel",
        [
            (EfileIrsAdapter, SubmissionChannel.EFILE_IRS.value),
            (EftpsAdapter, SubmissionChannel.EFTPS.value),
        ],
    )
    def test_execute_terminal_guard_blocks_when_irreversibility_passes(
        self, monkeypatch, adapter_cls, channel
    ):
        """Even if BOTH the policy enable AND the (future) irreversibility gate
        pass, AND a valid idempotency key is supplied, execute() STILL raises
        FeatureDisabled('channel not yet legally enabled'). This is the terminal
        dormant guard: the channel is scaffolding, never a live transmitter."""
        import app.submission.rail as rail_mod

        # Force the independent irreversibility gate to pass for this channel.
        monkeypatch.setattr(rail_mod, "irreversibility_ok", lambda ch, ctx=None: True)
        policy = _policy(channels={channel: True})
        adapter = adapter_cls(policy)
        with pytest.raises(FeatureDisabled) as exc:
            adapter.execute(_work_product(), idempotency_key="idem-123")
        assert exc.value.channel == channel
        assert "not yet legally enabled" in str(exc.value).lower()

    @pytest.mark.parametrize(
        "adapter_cls,channel",
        [
            (EfileIrsAdapter, SubmissionChannel.EFILE_IRS.value),
            (EftpsAdapter, SubmissionChannel.EFTPS.value),
        ],
    )
    def test_execute_without_idempotency_key_raises(self, adapter_cls, channel):
        policy = _policy(channels={channel: True})
        adapter = adapter_cls(policy)
        with pytest.raises(FeatureDisabled):
            adapter.execute(_work_product(), idempotency_key="")


# ---------------------------------------------------------------------------
# B. GET /submission/channels route shape
# ---------------------------------------------------------------------------


def _make_workspace(role: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=_WORKSPACE_ID,
        workspace_slug="acme",
        role=role,
        user_uc_uid="uc-test-user",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )


@pytest.fixture()
def patch_policy(monkeypatch):
    """Patch get_or_create_policy + neutralize workspace_session on the API module.

    Returns a setter so each test can choose the policy the handler will read.
    """
    state: dict = {"policy": _policy()}

    async def _fake_get_or_create_policy(session, workspace_id):
        return state["policy"]

    @asynccontextmanager
    async def _fake_workspace_session(session_factory, workspace_id):
        yield object()  # the handler only uses the session to call the (patched) policy fn

    monkeypatch.setattr(api_mod, "get_or_create_policy", _fake_get_or_create_policy)
    monkeypatch.setattr(api_mod, "workspace_session", _fake_workspace_session)

    def _set(policy: EngagementPolicyData) -> None:
        state["policy"] = policy

    return _set


def _client(role: str = "viewer") -> TestClient:
    app = FastAPI()
    app.include_router(api_mod.router)
    app.dependency_overrides[resolve_workspace] = lambda: _make_workspace(role)
    return TestClient(app)


def test_channels_listing_default_all_transmission_disabled(patch_policy):
    resp = _client("viewer").get(f"/api/workspaces/{_WORKSPACE_ID}/submission/channels")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_channel = {c["channel"]: c for c in body["channels"]}

    # paper_print is the only enabled channel (the default posture).
    assert by_channel["paper_print"]["enabled"] is True
    assert by_channel["paper_print"]["transmits"] is False

    # Every transmission channel is disabled by default.
    for channel in TRANSMISSION_CHANNELS:
        assert channel in by_channel, channel
        assert by_channel[channel]["enabled"] is False, channel
        assert by_channel[channel]["transmits"] is True, channel

    # Exactly one enabled channel overall == paper only.
    enabled = [c["channel"] for c in body["channels"] if c["enabled"]]
    assert enabled == ["paper_print"]
    assert body["rail_kill_switch"] is False


def test_channels_listing_requires_viewer_floor(patch_policy):
    # resolve_workspace is overridden to a role BELOW viewer is impossible (viewer
    # is the floor at rank 0); instead assert the route is reachable at viewer.
    resp = _client("viewer").get(f"/api/workspaces/{_WORKSPACE_ID}/submission/channels")
    assert resp.status_code == 200, resp.text

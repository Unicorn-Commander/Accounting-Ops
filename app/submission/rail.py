"""The dormant SubmissionRail: channels, gates, and adapters.

DESIGN POSTURE — *default OFF, paper only.* The platform takes a user all the way
to a finished work product; the only thing it actually *does* with that work
product today is tell you to print it and mail/hand it to the recipient
(``PaperPrintAdapter``). The four electronic transmission channels —

    efile_irs     IRS e-file (MeF / authorized e-file provider)
    state_portal  a state revenue department's online filing portal
    eftps         the Electronic Federal Tax Payment System (a money movement)
    esign         remote electronic signature of a finished document

— exist ONLY as dormant scaffolding. Each has a present-and-correct adapter, but
every method raises :class:`FeatureDisabled` unless THREE independent gates all
pass, and even then ``execute`` still raises ``FeatureDisabled`` — these are
seams, not live transmitters. The three gates are:

  1. **Policy enable** — the channel is explicitly enabled in the workspace's
     EngagementPolicy (``policy.channel_enabled(channel)``, which already folds in
     the global ``rail_kill_switch``). Default-deny: empty policy = every channel
     off.
  2. **Irreversibility threshold** — :func:`irreversibility_ok` is an *additional,
     independent* block. For every transmission channel it currently returns
     ``False`` (a TODO marks where a real per-org legal/risk gate goes). Even a
     policy-enabled channel cannot resolve unless this also passes.
  3. **Idempotency key** — a non-empty per-call ``idempotency_key`` must be
     supplied before any transmission adapter will act (so a retried call can
     never double-transmit / double-pay).

``PAPER_PRINT`` is NOT a transmission. It is always available, ignores the
kill-switch, needs no idempotency key, and is the always-on default. Nothing in
this module sends anything over a network — there is no client, no socket, no
provider call. It is structure, dormant.
"""
from __future__ import annotations

import enum
import logging
from typing import Any, Protocol, runtime_checkable

from app.engagement.policy import RAIL_CHANNELS as _POLICY_RAIL_CHANNELS

_log = logging.getLogger("acctops.submission.rail")


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class SubmissionChannel(str, enum.Enum):
    """Every channel the rail knows about.

    ``PAPER_PRINT`` is the always-on default (print + mail / hand to recipient).
    The other four are the gated electronic transmission channels and MUST match
    :data:`app.engagement.policy.RAIL_CHANNELS` exactly.
    """

    PAPER_PRINT = "paper_print"
    EFILE_IRS = "efile_irs"
    STATE_PORTAL = "state_portal"
    EFTPS = "eftps"
    ESIGN = "esign"


# The four gated transmission channels (everything except PAPER_PRINT). Kept as a
# tuple of the raw string values so it can be compared 1:1 with the policy module.
TRANSMISSION_CHANNELS: tuple[str, ...] = (
    SubmissionChannel.EFILE_IRS.value,
    SubmissionChannel.STATE_PORTAL.value,
    SubmissionChannel.EFTPS.value,
    SubmissionChannel.ESIGN.value,
)

# Invariant: the rail's gated channels are precisely the policy's RAIL_CHANNELS.
# If these ever drift, the policy gate (channel_enabled) and the rail would
# disagree about what "a transmission channel" is — fail loudly at import.
assert tuple(TRANSMISSION_CHANNELS) == tuple(_POLICY_RAIL_CHANNELS), (
    "submission.rail TRANSMISSION_CHANNELS must match engagement.policy "
    f"RAIL_CHANNELS; got {TRANSMISSION_CHANNELS!r} vs {tuple(_POLICY_RAIL_CHANNELS)!r}"
)

# Every channel, paper first. Useful for the /channels listing.
ALL_CHANNELS: tuple[str, ...] = (SubmissionChannel.PAPER_PRINT.value,) + TRANSMISSION_CHANNELS


def is_transmission_channel(channel: str) -> bool:
    """True if ``channel`` is one of the gated electronic transmission channels
    (i.e. not PAPER_PRINT)."""
    return channel in TRANSMISSION_CHANNELS


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FeatureDisabled(Exception):
    """Raised when a gated transmission channel is invoked while disabled.

    Carries the channel name + a human-readable reason so the API layer can turn
    it into a 403/409 with a clear message.
    """

    def __init__(self, message: str, *, channel: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.channel = channel


# ---------------------------------------------------------------------------
# Irreversibility gate (independent, additional block)
# ---------------------------------------------------------------------------


def irreversibility_ok(channel: str, context: dict[str, Any] | None = None) -> bool:
    """An independent risk gate, separate from the policy enable flag.

    Transmitting a return, filing through a state portal, moving money via EFTPS,
    or collecting a binding e-signature are all hard (or impossible) to take back.
    This is the seam where a real per-org legal/risk evaluation will live (is the
    org authorized as an e-file provider? is the amount within an approved EFTPS
    threshold? has counsel signed off for this jurisdiction?).

    For now it returns ``False`` for EVERY transmission channel — a deliberate,
    independent block on top of the policy flag, so that even a policy-enabled
    channel still cannot resolve. PAPER_PRINT is not a transmission and is always
    OK.

    TODO(legal-gate): replace the blanket ``return False`` with a real per-org
    legal/risk gate (provider authorization, per-channel monetary/irreversibility
    thresholds, jurisdiction sign-off) once the legal climate allows the rail to
    transmit. Until then this MUST stay default-False.
    """
    if channel == SubmissionChannel.PAPER_PRINT.value:
        return True
    # Dormant: no transmission channel passes the irreversibility threshold yet.
    return False


# ---------------------------------------------------------------------------
# Idempotency (echo-only, no durable store yet)
# ---------------------------------------------------------------------------

# Process-local set of keys we've "seen". This is intentionally NOT durable — it
# only exists so the no-op scaffolding can echo a key back. Nothing transmits, so
# there is nothing to actually de-duplicate yet.
#
# TODO(idempotency-store): back this with a durable, workspace-scoped store
# (a unique idempotency_key row + the recorded ack) before any channel goes live,
# so a retried transmit returns the original ack instead of acting twice.
_SEEN_IDEMPOTENCY_KEYS: set[str] = set()


def _record_idempotency_key(key: str) -> bool:
    """Record ``key`` in the (non-durable) seen-set. Returns True if newly seen,
    False if it was already present (a replay)."""
    if key in _SEEN_IDEMPOTENCY_KEYS:
        return False
    _SEEN_IDEMPOTENCY_KEYS.add(key)
    return True


def _machine_ack(
    channel: str,
    action: str,
    *,
    work_product_id: str | None,
    idempotency_key: str | None,
    transmitted: bool,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A uniform machine-confirmed acknowledgement structure for rail returns."""
    ack: dict[str, Any] = {
        "channel": channel,
        "action": action,
        "work_product_id": work_product_id,
        "idempotency_key": idempotency_key,
        "transmitted": transmitted,
        "detail": detail,
        "machine_confirmed": True,
    }
    if extra:
        ack.update(extra)
    return ack


# ---------------------------------------------------------------------------
# Work-product accessors (tolerant of a dict, an ORM row, or a mapping)
# ---------------------------------------------------------------------------


def _wp_attr(work_product: Any, name: str, default: Any = None) -> Any:
    if work_product is None:
        return default
    if isinstance(work_product, dict):
        return work_product.get(name, default)
    return getattr(work_product, name, default)


def _wp_id(work_product: Any) -> str | None:
    wid = _wp_attr(work_product, "id")
    return str(wid) if wid is not None else None


def _wp_document_pdf_path(work_product: Any) -> str:
    """The relative path to re-render + fetch the work product's filled PDF.

    Mirrors the work-products API route shape; the document is re-rendered on
    demand from the work product's ``inputs`` (never a stale snapshot). PaperPrint
    references this path so an operator can print exactly what the platform
    produced — no transmission.
    """
    wid = _wp_id(work_product)
    eid = _wp_attr(work_product, "entity_id")
    eid = str(eid) if eid is not None else "{entity_id}"
    wid = wid if wid is not None else "{work_product_id}"
    return f"entities/{eid}/work-products/{wid}/document.pdf"


# ---------------------------------------------------------------------------
# Rail protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SubmissionRail(Protocol):
    """A single submission channel.

    ``preflight`` reports readiness (no side effects). ``propose`` returns a
    human-confirmable description of what WOULD happen (no side effects).
    ``execute`` performs the action (and for every transmission channel currently
    raises :class:`FeatureDisabled` — dormant scaffolding).
    """

    channel: str

    def preflight(self, work_product: Any) -> dict[str, Any]:
        ...

    def propose(self, work_product: Any) -> dict[str, Any]:
        ...

    def execute(self, work_product: Any, idempotency_key: str) -> dict[str, Any]:
        ...


# ---------------------------------------------------------------------------
# PaperPrint — the always-on default (NOT a transmission)
# ---------------------------------------------------------------------------


class PaperPrintAdapter:
    """Print + mail / hand to recipient. ENABLED. The only live channel.

    This is today's behavior: the platform produces the filled PDF and the user
    prints it and delivers it. No network transmission of any kind. Ignores the
    kill-switch (it isn't a transmission) and needs no idempotency key.
    """

    channel = SubmissionChannel.PAPER_PRINT.value

    def preflight(self, work_product: Any) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "ready": True,
            "enabled": True,
            "transmits": False,
            "reason": "Paper printing is always available.",
            "document_pdf_path": _wp_document_pdf_path(work_product),
        }

    def propose(self, work_product: Any) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "transmits": False,
            "summary": "Print + mail / hand to recipient",
            "description": (
                "Print the filled PDF for this work product and mail it to — or "
                "hand it to — the recipient. Nothing is transmitted electronically."
            ),
            "document_pdf_path": _wp_document_pdf_path(work_product),
            "work_product_id": _wp_id(work_product),
        }

    def execute(self, work_product: Any, idempotency_key: str | None = None) -> dict[str, Any]:
        # No transmission. Return a print instruction referencing the (re-rendered
        # on demand) document.pdf path. idempotency_key is accepted + echoed but
        # not required for paper.
        pdf_path = _wp_document_pdf_path(work_product)
        return _machine_ack(
            self.channel,
            "print",
            work_product_id=_wp_id(work_product),
            idempotency_key=idempotency_key,
            transmitted=False,
            detail=f"Print this PDF and deliver it to the recipient: {pdf_path}",
            extra={"instruction": "print", "document_pdf_path": pdf_path},
        )


# ---------------------------------------------------------------------------
# Gated transmission adapters — present, but DORMANT
# ---------------------------------------------------------------------------


class _GatedTransmissionAdapter:
    """Shared base for the four dormant electronic transmission adapters.

    Each subclass sets ``channel`` + a human ``label`` + ``would_do`` text. Every
    method enforces the three gates and ``execute`` always raises FeatureDisabled
    — these are scaffolding, not live transmitters.
    """

    channel: str = ""
    label: str = ""
    would_do: str = ""

    # -- gate checks --------------------------------------------------------

    def _assert_policy_enabled(self, policy: Any) -> None:
        # policy.channel_enabled already folds in the global kill-switch.
        if policy is None or not policy.channel_enabled(self.channel):
            raise FeatureDisabled(
                f"{self.label} ({self.channel}) is disabled for this workspace "
                "(not enabled in the engagement policy, or the rail kill-switch "
                "is on). Default posture is OFF.",
                channel=self.channel,
            )

    def _assert_irreversibility_ok(self, context: dict[str, Any] | None = None) -> None:
        if not irreversibility_ok(self.channel, context):
            raise FeatureDisabled(
                f"{self.label} ({self.channel}) is blocked by the irreversibility "
                "threshold — an independent legal/risk gate that is not yet "
                "satisfied for this channel.",
                channel=self.channel,
            )

    def _assert_idempotency_key(self, idempotency_key: str | None) -> None:
        if not idempotency_key or not str(idempotency_key).strip():
            raise FeatureDisabled(
                f"{self.label} ({self.channel}) requires a per-call "
                "idempotency_key before it will act (so a retry can never "
                "double-transmit).",
                channel=self.channel,
            )

    def _assert_all_gates(
        self,
        policy: Any,
        *,
        idempotency_key: str | None = None,
        require_key: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Enforce: (1) policy enable, (2) irreversibility threshold,
        (3) idempotency key (only when ``require_key``)."""
        self._assert_policy_enabled(policy)
        self._assert_irreversibility_ok(context)
        if require_key:
            self._assert_idempotency_key(idempotency_key)

    # -- rail interface -----------------------------------------------------

    def __init__(self, policy: Any) -> None:
        # The adapter is bound to the resolving workspace's policy so every method
        # re-checks the gates against it (no method can act on a stale "enabled").
        self._policy = policy

    def preflight(self, work_product: Any) -> dict[str, Any]:
        # Gates first — a disabled channel is not "ready", it's blocked.
        self._assert_all_gates(self._policy)
        # Even if the first two gates somehow passed, the channel is dormant.
        raise FeatureDisabled(
            f"{self.label} ({self.channel}) is not yet legally enabled — dormant "
            "scaffolding only.",
            channel=self.channel,
        )

    def propose(self, work_product: Any) -> dict[str, Any]:
        self._assert_all_gates(self._policy)
        raise FeatureDisabled(
            f"{self.label} ({self.channel}) is not yet legally enabled — dormant "
            "scaffolding only.",
            channel=self.channel,
        )

    def execute(self, work_product: Any, idempotency_key: str) -> dict[str, Any]:
        # All three gates, including a required idempotency key.
        self._assert_all_gates(self._policy, idempotency_key=idempotency_key, require_key=True)
        # Even fully gated, the rail does not transmit yet. This is the dormant
        # invariant: the STRUCTURE exists; the action is a no-op-raise.
        raise FeatureDisabled(
            f"{self.label} ({self.channel}): channel not yet legally enabled.",
            channel=self.channel,
        )


class EfileIrsAdapter(_GatedTransmissionAdapter):
    channel = SubmissionChannel.EFILE_IRS.value
    label = "IRS e-file"
    would_do = "Transmit the finished federal return to the IRS via e-file."


class StatePortalAdapter(_GatedTransmissionAdapter):
    channel = SubmissionChannel.STATE_PORTAL.value
    label = "State portal filing"
    would_do = "File the finished return through the state revenue department's portal."


class EftpsAdapter(_GatedTransmissionAdapter):
    channel = SubmissionChannel.EFTPS.value
    label = "EFTPS payment"
    would_do = "Initiate a federal tax payment through EFTPS (moves money)."


class EsignAdapter(_GatedTransmissionAdapter):
    channel = SubmissionChannel.ESIGN.value
    label = "Electronic signature"
    would_do = "Collect a binding remote electronic signature on the finished document."


_GATED_ADAPTERS: dict[str, type[_GatedTransmissionAdapter]] = {
    EfileIrsAdapter.channel: EfileIrsAdapter,
    StatePortalAdapter.channel: StatePortalAdapter,
    EftpsAdapter.channel: EftpsAdapter,
    EsignAdapter.channel: EsignAdapter,
}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def resolve_adapter(channel: str, policy: Any) -> SubmissionRail:
    """Return the :class:`SubmissionRail` adapter for ``channel``, enforcing gates.

    * ``paper_print`` ALWAYS resolves to :class:`PaperPrintAdapter` (ignores the
      kill-switch; it isn't a transmission).
    * Each transmission channel resolves to its adapter ONLY when both the policy
      enable flag (``policy.channel_enabled`` — which folds in the kill-switch)
      AND the independent :func:`irreversibility_ok` gate pass. Otherwise it
      raises :class:`FeatureDisabled`. Even when it resolves, the adapter's
      ``execute`` is a no-op-raise for now (dormant scaffolding).
    * An unknown channel raises :class:`FeatureDisabled`.
    """
    if channel == SubmissionChannel.PAPER_PRINT.value:
        return PaperPrintAdapter()

    adapter_cls = _GATED_ADAPTERS.get(channel)
    if adapter_cls is None:
        raise FeatureDisabled(f"Unknown submission channel {channel!r}.", channel=channel)

    # Global kill-switch: blocks every transmission channel outright. (PaperPrint,
    # handled above, is never affected — it isn't a transmission.)
    if policy is not None and getattr(policy, "rail_kill_switch", False):
        raise FeatureDisabled(
            f"{adapter_cls.label} ({channel}) is blocked: the submission-rail "
            "kill-switch is engaged.",
            channel=channel,
        )

    # Gate 1: explicit policy enable (also re-checks the kill-switch).
    if policy is None or not policy.channel_enabled(channel):
        raise FeatureDisabled(
            f"{adapter_cls.label} ({channel}) is disabled for this workspace "
            "(not enabled in the engagement policy). Default posture is OFF.",
            channel=channel,
        )

    # Gate 2: independent irreversibility threshold (currently always False here).
    if not irreversibility_ok(channel):
        raise FeatureDisabled(
            f"{adapter_cls.label} ({channel}) is blocked by the irreversibility "
            "threshold (an independent legal/risk gate not yet satisfied).",
            channel=channel,
        )

    # Even fully gated-on, the adapter's execute() remains a no-op-raise. The
    # structure exists; nothing transmits yet.
    return adapter_cls(policy)

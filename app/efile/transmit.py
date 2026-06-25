"""The GATED transmit interface — the package is real, the wire is OFF.

:func:`transmit` is the single seam where a live IRS/state Modernized e-File (MeF)
client would eventually go. Today it ALWAYS raises :class:`EfileTransmitGated`.
That is intentional and honest: :mod:`app.efile.mef` produces a genuine,
submission-shaped package, but actually putting it on the wire requires the org's
own **EFIN** (Electronic Filing Identification Number) and A2A/IFA transmitter
enrollment with the IRS — a real legal gate, not a feature flag we can flip.

Triple gate
-----------
``transmit`` raises ``EfileTransmitGated`` unless ALL THREE of the following pass,
and EVEN THEN it still raises (the wire is not implemented):

  (a) **Policy enable** — the workspace ``EngagementPolicy`` enables the channel
      (``policy.channel_enabled(channel)``, which folds in the rail kill-switch).
      Default-deny.
  (b) **Real EFIN** — a non-empty EFIN is supplied (the org's IRS-issued
      Electronic Filing Identification Number; a 6-digit identifier in practice).
  (c) **Explicit env flag** — ``EFILE_TRANSMIT_ENABLED`` is truthy in the process
      environment.

When (a)+(b)+(c) all pass, ``transmit`` STILL raises
``EfileTransmitGated("live IRS/state transmission requires registered-transmitter
enrollment; package is ready, transmission not yet enabled")``. This is the
dormant invariant — the same posture as :mod:`app.submission.rail`: the structure
exists; nothing transmits.

Wiring a real transmitter
-------------------------
A live MeF A2A/IFA client (sign + zip the package into a submission, POST to the
IRS transmission endpoint under the org's EFIN, poll for the acknowledgement) is
implemented in the final branch below — replacing the last ``raise`` with the real
send — only once the org is an authorized transmitter and the legal gate allows
it. This module never opens a socket or calls a provider today.

This module never calls ``datetime.now()`` or ``random`` and never edits config.
The env flag is read directly via ``os.getenv`` at call time so a deployment can
gate it without a code change.
"""
from __future__ import annotations

import os
from typing import Any

# Env flag (read at call time so monkeypatch/redeploy can toggle it). Truthy set
# is intentionally narrow + case-insensitive.
_ENV_TRANSMIT_FLAG = "EFILE_TRANSMIT_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}


class EfileTransmitGated(Exception):
    """Raised when e-file transmission is invoked while the wire is gated/off.

    Carries the ``channel`` and a machine-readable ``reason`` (``policy_disabled``
    / ``no_efin`` / ``env_disabled`` / ``not_implemented``) so an API layer can map
    it to a clear 403/409 with an actionable message.
    """

    def __init__(
        self, message: str, *, channel: str | None = None, reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.channel = channel
        self.reason = reason


def _env_transmit_enabled() -> bool:
    return os.getenv(_ENV_TRANSMIT_FLAG, "").strip().lower() in _TRUTHY


def _efin_supplied(efin: str | None) -> bool:
    """True only if a non-empty EFIN string is provided. (A real EFIN is the org's
    IRS-issued 6-digit identifier; we require it be present, not validate it here —
    validation belongs to the transmitter-enrollment step.)"""
    return bool(efin and str(efin).strip())


def transmit(package: Any, *, channel: str, efin: str | None, policy: Any) -> dict:
    """Attempt to transmit a generated e-file ``package``. ALWAYS raises today.

    Enforces the triple gate (policy enable -> real EFIN -> env flag) and, even
    when all three pass, raises :class:`EfileTransmitGated` because the live MeF
    wire is not implemented (dormant invariant). The signature returns ``dict`` to
    document the eventual acknowledgement shape (a submission id + IRS ack) once a
    real transmitter is wired in the final branch below.

    Parameters
    ----------
    package:
        An :class:`app.efile.mef.EfilePackage` (or anything with the same shape).
        Not transmitted today; carried so the future wire has the bytes + manifest.
    channel:
        A submission-rail channel value (e.g. ``"efile_irs"`` / ``"state_portal"``).
    efin:
        The org's IRS-issued EFIN. Required for gate (b).
    policy:
        The workspace ``EngagementPolicyData`` (duck-typed: must expose
        ``channel_enabled(channel) -> bool``). Required for gate (a).

    Raises
    ------
    EfileTransmitGated
        Always — with ``reason`` indicating which gate stopped it, or
        ``not_implemented`` when all gates pass but the wire is still off.
    """
    # Gate (a): explicit policy enable (folds in the rail kill-switch).
    enabled = (
        policy is not None
        and callable(getattr(policy, "channel_enabled", None))
        and bool(policy.channel_enabled(channel))
    )
    if not enabled:
        raise EfileTransmitGated(
            f"e-file channel {channel!r} is not enabled in the workspace "
            "EngagementPolicy (default-deny). Transmission refused.",
            channel=channel,
            reason="policy_disabled",
        )

    # Gate (b): a real EFIN must be supplied.
    if not _efin_supplied(efin):
        raise EfileTransmitGated(
            "no registered EFIN supplied; live e-file transmission requires the "
            "org's IRS-issued Electronic Filing Identification Number (EFIN).",
            channel=channel,
            reason="no_efin",
        )

    # Gate (c): explicit environment opt-in.
    if not _env_transmit_enabled():
        raise EfileTransmitGated(
            f"{_ENV_TRANSMIT_FLAG} is not set; the e-file transmit wire is off by "
            "configuration.",
            channel=channel,
            reason="env_disabled",
        )

    # All three gates pass — but the wire is STILL off. This is where a live MeF
    # A2A/IFA client would sign + submit the package under the org's EFIN and
    # return the IRS acknowledgement. Until that exists (and the legal gate
    # allows it), this remains a no-op-raise: the dormant invariant.
    raise EfileTransmitGated(
        "live IRS/state transmission requires registered-transmitter enrollment; "
        "package is ready, transmission not yet enabled",
        channel=channel,
        reason="not_implemented",
    )

"""Dormant, default-OFF submission rail.

The platform produces finished, submission-ready work products (filled W-4P /
ST-3 / 1040 / CP504 response, …). This package is the *structure* that could one
day transmit them electronically (IRS e-file / a state portal / EFTPS / e-sign),
but it ships DORMANT: the only enabled channel is ``PAPER_PRINT`` (print + mail /
hand to the recipient — today's behavior). Every transmission channel is present
as scaffolding and refuses to act unless explicitly, independently gated on, and
even then their ``execute`` is a deliberate no-op-raise ("not yet legally
enabled"). The point is that the seams exist so the rail CAN be turned on later if
the legal climate allows — not that anything transmits today.

See :mod:`app.submission.rail` for the channels, gates, and adapters.
"""

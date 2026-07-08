"""Outbound mail integration.

Strategy
~~~~~~~~
The platform's mail relay is **Stalwart on kvm2-mail** (mail.unicorncommander.ai).
This module attempts SMTP submission via env-configured creds. On ANY failure
(connection refused, auth error, timeout) or when no relay is configured, the
message is durably queued in ``public.email_outbox`` for a follow-up
integration to drain. This means a workspace invitation NEVER fails for mail
reasons — the row is always created and either delivered or queued.

Env knobs (all optional; absence = queue-only mode):

  SMTP_HOST       e.g. mail.unicorncommander.ai
  SMTP_PORT       587 (default; submission with STARTTLS)
  SMTP_USERNAME   e.g. noreply@accounting-ops.magicunicorn.dev
  SMTP_PASSWORD   bound from a secret
  SMTP_FROM       defaults to SMTP_USERNAME
  SMTP_USE_TLS    "true" (default) — STARTTLS on 587 / direct TLS on 465
  SMTP_TIMEOUT_S  default 10
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("acctops.mailer")


def _recipient_domain(addr: str | None) -> str:
    """Audit 2026-05-31 Bug 16: log only the recipient DOMAIN, never the
    local-part. Full address + subject stay in the ``email_outbox`` row
    (auditable, but not in stdout where CPA/customer PII could leak into log
    aggregators). Returns "<none>" / "<malformed>" for missing or wrong-shape
    inputs rather than raising — the mailer must never fail because of logging.
    """
    if not addr:
        return "<none>"
    s = str(addr).strip()
    if "@" not in s:
        return "<malformed>"
    # rsplit so display-name email like 'Jane <jane@example.com>' stays robust.
    return s.rsplit("@", 1)[-1].rstrip(">").lower() or "<malformed>"


@dataclass(frozen=True)
class SmtpConfig:
    host: str | None
    port: int
    username: str | None
    password: str | None
    from_addr: str
    use_tls: bool
    timeout_s: int

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.from_addr)


def load_smtp_config() -> SmtpConfig:
    host = os.getenv("SMTP_HOST") or None
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME") or None
    password = os.getenv("SMTP_PASSWORD") or None
    from_addr = (
        os.getenv("SMTP_FROM")
        or username
        or "noreply@accounting-ops.magicunicorn.dev"
    )
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes"}
    timeout_s = int(os.getenv("SMTP_TIMEOUT_S", "10"))
    return SmtpConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        from_addr=from_addr,
        use_tls=use_tls,
        timeout_s=timeout_s,
    )


def _smtp_send_sync(cfg: SmtpConfig, msg: EmailMessage) -> None:
    """Blocking SMTP send. Called via asyncio.to_thread from the async helper."""
    assert cfg.host is not None  # guarded by caller
    if cfg.port == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            cfg.host, cfg.port, timeout=cfg.timeout_s, context=context
        ) as s:
            if cfg.username and cfg.password:
                s.login(cfg.username, cfg.password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_s) as s:
            if cfg.use_tls:
                s.starttls(context=ssl.create_default_context())
            if cfg.username and cfg.password:
                s.login(cfg.username, cfg.password)
            s.send_message(msg)


async def send_email(
    session: AsyncSession,
    *,
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    related_invitation_id: UUID | None = None,
) -> dict[str, Any]:
    """Send an email via SMTP; on failure / missing config, queue in email_outbox.

    Returns ``{"status": "sent"|"queued", "outbox_id": <uuid>, "detail": str}``.
    Always inserts an email_outbox row so the lifecycle is auditable.
    """
    import asyncio
    from uuid import uuid4

    cfg = load_smtp_config()
    outbox_id = uuid4()

    # Pre-insert as pending; we'll update status after the attempt.
    await session.execute(
        text(
            """
            INSERT INTO public.email_outbox (
                id, to_email, from_email, subject, body_text, body_html,
                status, related_invitation_id, attempt_count
            ) VALUES (
                :id, :to, :from, :subject, :body_text, :body_html,
                'pending', :rel, 0
            )
            """
        ),
        {
            "id": str(outbox_id),
            "to": to_email,
            "from": cfg.from_addr,
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
            "rel": str(related_invitation_id) if related_invitation_id else None,
        },
    )

    if not cfg.is_configured:
        # Queue-only mode. No SMTP relay configured; the row stays pending for
        # the follow-up integration to drain.
        await session.execute(
            text(
                """
                UPDATE public.email_outbox
                   SET last_error = 'SMTP not configured (SMTP_HOST unset)',
                       attempt_count = attempt_count + 1
                 WHERE id = :id
                """
            ),
            {"id": str(outbox_id)},
        )
        # Audit 2026-05-31 Bug 16: log outbox id + domain + status only; the
        # full to_email + subject live in the email_outbox row for audit.
        _log.info(
            "[mailer] queued (no SMTP relay) outbox=%s to_domain=%s status=queued",
            outbox_id, _recipient_domain(to_email),
        )
        return {
            "status": "queued",
            "outbox_id": str(outbox_id),
            "detail": "SMTP not configured; queued in email_outbox for follow-up",
        }

    msg = EmailMessage()
    msg["From"] = cfg.from_addr
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    try:
        await asyncio.to_thread(_smtp_send_sync, cfg, msg)
    except Exception as exc:  # noqa: BLE001 — we WANT every failure caught
        err = f"{exc.__class__.__name__}: {exc}"
        await session.execute(
            text(
                """
                UPDATE public.email_outbox
                   SET last_error = :err,
                       attempt_count = attempt_count + 1
                 WHERE id = :id
                """
            ),
            {"id": str(outbox_id), "err": err[:4000]},
        )
        # Audit 2026-05-31 Bug 16: log outbox id + domain + status; never
        # full email or subject in stdout.
        _log.warning(
            "[mailer] SMTP send failed; queued outbox=%s to_domain=%s status=queued err=%s",
            outbox_id, _recipient_domain(to_email), err,
        )
        return {
            "status": "queued",
            "outbox_id": str(outbox_id),
            "detail": f"SMTP send failed: {err}; queued for retry",
        }

    await session.execute(
        text(
            """
            UPDATE public.email_outbox
               SET status = 'sent',
                   sent_at = now(),
                   attempt_count = attempt_count + 1
             WHERE id = :id
            """
        ),
        {"id": str(outbox_id)},
    )
    # Audit 2026-05-31 Bug 16: log outbox id + domain + status; full email +
    # subject stay in the email_outbox row only.
    _log.info(
        "[mailer] SMTP send OK outbox=%s to_domain=%s status=sent",
        outbox_id, _recipient_domain(to_email),
    )
    return {
        "status": "sent",
        "outbox_id": str(outbox_id),
        "detail": "delivered via SMTP",
    }


__all__ = ("send_email", "load_smtp_config", "SmtpConfig")

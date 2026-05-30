"""Drain the public.email_outbox queue via SMTP.

Run modes
~~~~~~~~~
* ``python -m scripts.drain_email_outbox`` — single pass; drains every
  ``status='pending'`` row eligible for retry, then exits. Use this for ad-hoc
  cleanup or from cron-style triggers.
* ``python -m scripts.drain_email_outbox --loop`` — long-running loop with a
  short sleep between passes (``--interval-s``, default 60s). Use this as the
  process entry point inside the dedicated ``mail-drainer`` compose service.

Behavior
~~~~~~~~
* Reads ``SMTP_*`` env vars via :mod:`app.mailer` — identical to live invite
  send-time, so behavior is symmetric.
* Selects ``id, to_email, from_email, subject, body_text, body_html,
  attempt_count`` from ``public.email_outbox`` where ``status = 'pending'``
  ``AND attempt_count < MAX_ATTEMPTS`` (default 5), ordered by ``queued_at``
  ASC, with ``FOR UPDATE SKIP LOCKED`` so two drainer pods can run safely
  side-by-side and never double-send a row.
* On send success: ``UPDATE ... SET status='sent', sent_at=now(),
  attempt_count = attempt_count + 1``.
* On send failure: ``UPDATE ... SET last_error=:err,
  attempt_count = attempt_count + 1``; if ``attempt_count + 1 >= MAX_ATTEMPTS``,
  also flips to ``status='failed'`` (terminal — no further retries).
* Rows that already have ``status != 'pending'`` are NEVER touched (idempotent;
  cannot double-send a previously-sent row).

Connection
~~~~~~~~~~
* Prefers ``ADMIN_DATABASE_URL`` (the owner role), falls back to
  ``DATABASE_URL`` (the runtime app role, which has
  ``SELECT, INSERT, UPDATE`` on ``email_outbox``).
* If the URL uses the ``+asyncpg`` driver, that suffix is stripped — this
  script is intentionally synchronous so each row commits in its own short
  transaction, freeing the row lock immediately.

Safety
~~~~~~
* Per-row commit + ``SKIP LOCKED`` make this safe under concurrent execution.
* MAX_ATTEMPTS caps retries (failed rows stop being touched).
* SMTP_PASSWORD is never logged.
"""
from __future__ import annotations

import argparse
import logging
import os
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

_log = logging.getLogger("acctops.mail-drainer")

MAX_ATTEMPTS = int(os.getenv("MAIL_DRAINER_MAX_ATTEMPTS", "5"))
BATCH_LIMIT = int(os.getenv("MAIL_DRAINER_BATCH_LIMIT", "25"))


@dataclass(frozen=True)
class SmtpConfig:
    host: str | None
    port: int
    username: str | None
    password: str | None
    use_tls: bool
    timeout_s: int

    @property
    def is_configured(self) -> bool:
        return bool(self.host)


def load_smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host=os.getenv("SMTP_HOST") or None,
        port=int(os.getenv("SMTP_PORT", "587")),
        username=os.getenv("SMTP_USERNAME") or None,
        password=os.getenv("SMTP_PASSWORD") or None,
        use_tls=os.getenv("SMTP_USE_TLS", "true").strip().lower()
        in {"1", "true", "yes"},
        timeout_s=int(os.getenv("SMTP_TIMEOUT_S", "10")),
    )


def _resolve_database_url() -> str:
    """Prefer the owner role for the drainer (less likely to hit RLS surprises)."""
    url = os.getenv("ADMIN_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit(
            "drain_email_outbox: neither ADMIN_DATABASE_URL nor DATABASE_URL is set"
        )
    # Make it synchronous regardless of how the app was configured.
    return url.replace("+asyncpg", "")


def _build_engine() -> Engine:
    return create_engine(
        _resolve_database_url(),
        pool_pre_ping=True,
        future=True,
    )


def _smtp_send(cfg: SmtpConfig, msg: EmailMessage) -> None:
    """Blocking SMTP send. Raises on any failure."""
    assert cfg.host is not None
    if cfg.port == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            cfg.host, cfg.port, timeout=cfg.timeout_s, context=context
        ) as s:
            if cfg.username and cfg.password:
                s.login(cfg.username, cfg.password)
            s.send_message(msg)
        return
    with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_s) as s:
        if cfg.use_tls:
            s.starttls(context=ssl.create_default_context())
        if cfg.username and cfg.password:
            s.login(cfg.username, cfg.password)
        s.send_message(msg)


def _build_message(row: dict[str, Any]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = row["from_email"]
    msg["To"] = row["to_email"]
    msg["Subject"] = row["subject"]
    msg.set_content(row["body_text"] or "")
    if row.get("body_html"):
        msg.add_alternative(row["body_html"], subtype="html")
    return msg


def _claim_batch(conn, limit: int) -> list[dict[str, Any]]:
    """Lock and return at most ``limit`` pending rows.

    ``FOR UPDATE SKIP LOCKED`` lets multiple drainer instances cooperate
    without ever picking the same row. Caller is responsible for the
    transaction lifecycle.
    """
    rows = conn.execute(
        text(
            """
            SELECT id, to_email, from_email, subject, body_text, body_html,
                   attempt_count
              FROM public.email_outbox
             WHERE status = 'pending'
               AND attempt_count < :max_attempts
             ORDER BY queued_at ASC
             LIMIT :limit
             FOR UPDATE SKIP LOCKED
            """
        ),
        {"max_attempts": MAX_ATTEMPTS, "limit": limit},
    ).mappings().all()
    return [dict(r) for r in rows]


def _mark_sent(conn, row_id: str) -> None:
    conn.execute(
        text(
            """
            UPDATE public.email_outbox
               SET status = 'sent',
                   sent_at = now(),
                   attempt_count = attempt_count + 1,
                   last_error = NULL
             WHERE id = :id
               AND status = 'pending'
            """
        ),
        {"id": row_id},
    )


def _mark_attempt_failed(
    conn, row_id: str, err: str, current_attempts: int
) -> bool:
    """Record a failed attempt. Returns ``True`` if the row is now terminal."""
    new_count = current_attempts + 1
    terminal = new_count >= MAX_ATTEMPTS
    if terminal:
        conn.execute(
            text(
                """
                UPDATE public.email_outbox
                   SET status = 'failed',
                       last_error = :err,
                       attempt_count = :n
                 WHERE id = :id
                   AND status = 'pending'
                """
            ),
            {"id": row_id, "err": err[:4000], "n": new_count},
        )
    else:
        conn.execute(
            text(
                """
                UPDATE public.email_outbox
                   SET last_error = :err,
                       attempt_count = :n
                 WHERE id = :id
                   AND status = 'pending'
                """
            ),
            {"id": row_id, "err": err[:4000], "n": new_count},
        )
    return terminal


def _drain_once(engine: Engine, cfg: SmtpConfig) -> dict[str, int]:
    """One pass: claim + send a batch of pending rows.

    Each row gets its own short transaction so a single bad row never blocks
    the rest of the queue, and the SKIP-LOCKED lease is released the moment
    that row is processed.
    """
    if not cfg.is_configured:
        _log.warning("drain skipped: SMTP_HOST is unset (no relay configured)")
        return {"sent": 0, "failed": 0, "terminal": 0, "considered": 0}

    sent = failed = terminal = considered = 0

    # Claim a small batch first (single short transaction), then process each
    # row in its own transaction to keep lock-windows tight.
    with engine.connect() as scout_conn:
        with scout_conn.begin():
            batch = _claim_batch(scout_conn, BATCH_LIMIT)
        # NOTE: COMMIT here drops the row locks — that's the whole point of
        # this two-phase pattern. Each row below is re-claimed under a fresh
        # SKIP LOCKED, so a parallel drainer that races in between the two
        # phases simply takes the row instead of double-sending it.

    if not batch:
        return {"sent": 0, "failed": 0, "terminal": 0, "considered": 0}

    for row in batch:
        considered += 1
        rid = row["id"]
        # Re-claim under a fresh, narrow transaction to send + mark atomically.
        with engine.connect() as conn:
            with conn.begin():
                claimed = conn.execute(
                    text(
                        """
                        SELECT attempt_count
                          FROM public.email_outbox
                         WHERE id = :id
                           AND status = 'pending'
                           AND attempt_count < :max_attempts
                         FOR UPDATE SKIP LOCKED
                        """
                    ),
                    {"id": rid, "max_attempts": MAX_ATTEMPTS},
                ).mappings().first()
                if not claimed:
                    # Another drainer or a status flip won the race; skip.
                    continue
                current_attempts = claimed["attempt_count"]
                try:
                    msg = _build_message(row)
                    _smtp_send(cfg, msg)
                except Exception as exc:  # noqa: BLE001
                    err = f"{exc.__class__.__name__}: {exc}"
                    was_terminal = _mark_attempt_failed(
                        conn, rid, err, current_attempts
                    )
                    failed += 1
                    if was_terminal:
                        terminal += 1
                    _log.warning(
                        "send failed id=%s to=%s err=%s terminal=%s",
                        rid, row["to_email"], err, was_terminal,
                    )
                else:
                    _mark_sent(conn, str(rid))
                    sent += 1
                    _log.info(
                        "sent id=%s to=%s subject=%r",
                        rid, row["to_email"], row["subject"],
                    )

    return {
        "sent": sent,
        "failed": failed,
        "terminal": terminal,
        "considered": considered,
    }


def _run_loop(engine: Engine, cfg: SmtpConfig, interval_s: int) -> None:
    """Long-running loop. Sleeps ``interval_s`` between passes."""
    _log.info(
        "drainer loop started host=%s port=%s interval_s=%s max_attempts=%s",
        cfg.host, cfg.port, interval_s, MAX_ATTEMPTS,
    )
    while True:
        try:
            summary = _drain_once(engine, cfg)
            if summary["considered"]:
                _log.info("pass complete %s", summary)
        except KeyboardInterrupt:
            _log.info("drainer interrupted; exiting cleanly")
            return
        except Exception:  # noqa: BLE001
            _log.exception("drain pass crashed; will retry next interval")
        time.sleep(interval_s)


def _summarize_initial_state(engine: Engine) -> None:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT status, count(*)::int AS n
                  FROM public.email_outbox
                 GROUP BY status
                 ORDER BY status
                """
            )
        ).mappings().all()
    _log.info(
        "email_outbox initial state: %s",
        {r["status"]: r["n"] for r in rows} or "empty",
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drain public.email_outbox via SMTP.")
    parser.add_argument(
        "--loop", action="store_true",
        help="Run forever; drain every --interval-s seconds.",
    )
    parser.add_argument(
        "--interval-s", type=int, default=int(os.getenv("MAIL_DRAINER_INTERVAL_S", "60")),
        help="Loop sleep interval (only with --loop). Default 60s.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG logging.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = load_smtp_config()
    engine = _build_engine()
    _summarize_initial_state(engine)

    if args.loop:
        _run_loop(engine, cfg, args.interval_s)
        return 0

    summary = _drain_once(engine, cfg)
    _log.info("single-pass complete %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

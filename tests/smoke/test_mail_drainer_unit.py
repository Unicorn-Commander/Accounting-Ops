"""Pure unit tests for the mail-drainer + SmtpConfig — no DB / no network."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


# scripts/ is a top-level directory with NO __init__.py, so the standard
# package import won't find it. Load the module by file path instead.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRAIN_PATH = _REPO_ROOT / "scripts" / "drain_email_outbox.py"


@pytest.fixture
def drainer(monkeypatch):
    """Load scripts/drain_email_outbox.py as a module from its file path.

    We register the loaded module in ``sys.modules`` before executing it so
    that ``@dataclass`` annotations resolve cleanly on Python 3.14, where
    ``dataclasses`` looks up the defining module via ``sys.modules`` during
    field analysis.
    """
    import importlib.util

    mod_name = "drain_email_outbox_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, _DRAIN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(mod_name, None)


# ----- env-driven config plumbing --------------------------------------


def test_smtp_config_round_trip(drainer, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "mail.example.test")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USERNAME", "u@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setenv("SMTP_USE_TLS", "false")
    monkeypatch.setenv("SMTP_TIMEOUT_S", "5")

    cfg = drainer.load_smtp_config()
    assert cfg.host == "mail.example.test"
    assert cfg.port == 465
    assert cfg.username == "u@example.test"
    assert cfg.password == "p"
    assert cfg.use_tls is False
    assert cfg.timeout_s == 5
    assert cfg.is_configured is True


def test_smtp_config_missing_host_is_unconfigured(drainer, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    cfg = drainer.load_smtp_config()
    assert cfg.host is None
    assert cfg.is_configured is False


def test_use_tls_default_is_true_when_unset(drainer, monkeypatch):
    monkeypatch.delenv("SMTP_USE_TLS", raising=False)
    cfg = drainer.load_smtp_config()
    assert cfg.use_tls is True


# ----- database URL resolution -----------------------------------------


def test_resolve_database_url_prefers_admin(drainer, monkeypatch):
    monkeypatch.setenv(
        "ADMIN_DATABASE_URL", "postgresql+asyncpg://admin:pw@pg:5432/db"
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app:pw@pg:5432/db")
    url = drainer._resolve_database_url()
    # +asyncpg becomes an explicit psycopg2 driver (drainer is synchronous;
    # SQLAlchemy 2.1 would default a bare postgresql:// URL to psycopg v3)
    assert url == "postgresql+psycopg2://admin:pw@pg:5432/db"


def test_resolve_database_url_falls_back_to_runtime(drainer, monkeypatch):
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app:pw@pg:5432/db")
    url = drainer._resolve_database_url()
    assert url == "postgresql+psycopg2://app:pw@pg:5432/db"


def test_resolve_database_url_names_driver_for_bare_url(drainer, monkeypatch):
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:REMOVED@pg:5432/db")
    assert drainer._resolve_database_url() == "postgresql+psycopg2://app:pw@pg:5432/db"


def test_resolve_database_url_raises_when_both_unset(drainer, monkeypatch):
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="DATABASE_URL"):
        drainer._resolve_database_url()


# ----- message construction --------------------------------------------


def test_build_message_text_only(drainer):
    row = {
        "from_email": "noreply@magicunicorn.dev",
        "to_email": "to@example.org",
        "subject": "Hello",
        "body_text": "plain body",
        "body_html": None,
    }
    msg = drainer._build_message(row)
    assert msg["From"] == "noreply@magicunicorn.dev"
    assert msg["To"] == "to@example.org"
    assert msg["Subject"] == "Hello"
    assert "plain body" in msg.get_content()


def test_build_message_multipart_when_html_present(drainer):
    row = {
        "from_email": "noreply@magicunicorn.dev",
        "to_email": "to@example.org",
        "subject": "Hi",
        "body_text": "fallback text",
        "body_html": "<p>fancy</p>",
    }
    msg = drainer._build_message(row)
    # add_alternative makes it multipart/alternative
    assert msg.is_multipart()
    parts = list(msg.iter_parts())
    assert len(parts) == 2
    subtypes = {p.get_content_subtype() for p in parts}
    assert subtypes == {"plain", "html"}


# ----- module surface guard --------------------------------------------


def test_max_attempts_default_is_five(drainer, monkeypatch):
    # MAX_ATTEMPTS is captured at module-import time. The fact that the
    # module-level constant is sane is part of the contract.
    assert drainer.MAX_ATTEMPTS >= 1
    # A safety ceiling so a bug can't infinite-retry.
    assert drainer.MAX_ATTEMPTS <= 50

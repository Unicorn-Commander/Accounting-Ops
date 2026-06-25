"""E-signature provider abstraction + concrete providers.

WHY THIS IS A *REAL* (not dormant) CAPABILITY
==============================================
E-signature here is the *taxpayer's own* signature on the *taxpayer's own*
document (a filled W-4P / 1040 / engagement letter, …). There is no
unauthorized-practice-of-law concern and no IRS transmission — nothing is filed
with a tax authority. So, unlike the dormant IRS e-FILE / EFTPS rail in
``app/submission/rail.py`` (which stays legally gated and never transmits), the
e-signature capability is genuinely usable. It is still gated behind a
*per-workspace* feature flag (see ``app/api/esign.py``) so a workspace opts in
explicitly.

PROVIDERS
=========
``EsignProvider`` is a structural ``typing.Protocol`` with three coroutines:

    create_request(document_bytes, signer_name, signer_email, subject)
        -> {"external_id": str|None, "signing_url": str|None, "status": str}
    get_status(external_id) -> status            (one of SIGNATURE_STATUSES)
    fetch_signed(external_id) -> bytes | None     (the completed PDF, if any)

Two concrete implementations:

  * ``DocusealProvider`` — talks to a self-hostable `Docuseal
    <https://www.docuseal.com/>`_ instance over its REST API (create a template
    from the PDF, open a submission, poll status, download the completed PDF).
    Configured via the env vars ESIGN_DOCUSEAL_BASE_URL + ESIGN_DOCUSEAL_API_KEY
    (read here with ``os.getenv`` — deliberately NOT added to app/config.py to
    avoid a merge collision with parallel work). Any network / HTTP failure or
    absent server raises :class:`ProviderUnavailable` with a clear message — it
    never fails silently.

  * ``ManualProvider`` — the always-available fallback that needs NO external
    server. ``create_request`` simply marks the request as awaiting a wet /
    manual signature and returns no ``signing_url``; the workflow is then
    download → sign → re-upload the signed PDF (see the ``mark-signed`` route).
    This keeps e-sign usable even with no Docuseal instance configured.

``resolve_provider`` returns the Docuseal provider when BOTH env vars are set,
otherwise the Manual provider.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Mapping, Protocol, runtime_checkable

import httpx

_log = logging.getLogger("acctops.esign.provider")


# ---------------------------------------------------------------------------
# Status vocabulary (the SignatureRequest lifecycle)
# ---------------------------------------------------------------------------

STATUS_CREATED = "created"
STATUS_SENT = "sent"
STATUS_OUT_FOR_SIGNATURE = "out_for_signature"
STATUS_SIGNED = "signed"
STATUS_DECLINED = "declined"
STATUS_CANCELED = "canceled"

#: Every legal status for a :class:`app.models.ledger.SignatureRequest` row.
#: Mirrors the DB CHECK constraint ``ck_signature_request_status``.
SIGNATURE_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_CREATED,
        STATUS_SENT,
        STATUS_OUT_FOR_SIGNATURE,
        STATUS_SIGNED,
        STATUS_DECLINED,
        STATUS_CANCELED,
    }
)

#: Terminal states — a request in one of these never advances again.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_SIGNED, STATUS_DECLINED, STATUS_CANCELED}
)


def coerce_status(raw: object, *, default: str = STATUS_SENT) -> str:
    """Normalize an arbitrary provider-reported status into the vocab.

    Anything already in :data:`SIGNATURE_STATUSES` is returned as-is; anything
    else falls back to ``default`` (so a provider that invents a new status can
    never write an illegal value past the DB CHECK constraint)."""
    value = (str(raw) if raw is not None else "").strip().lower()
    return value if value in SIGNATURE_STATUSES else default


# Map Docuseal's submitter/submission status strings onto our vocabulary.
_DOCUSEAL_STATUS_MAP: dict[str, str] = {
    "completed": STATUS_SIGNED,
    "signed": STATUS_SIGNED,
    "declined": STATUS_DECLINED,
    "opened": STATUS_OUT_FOR_SIGNATURE,
    "viewed": STATUS_OUT_FOR_SIGNATURE,
    "in_progress": STATUS_OUT_FOR_SIGNATURE,
    "awaiting": STATUS_SENT,
    "pending": STATUS_SENT,
    "sent": STATUS_SENT,
}


def normalize_docuseal_status(raw: object) -> str:
    """Map a Docuseal status onto the vocab. Unknown/blank -> out_for_signature
    (a safe in-flight default — never silently 'signed')."""
    if raw is None:
        return STATUS_OUT_FOR_SIGNATURE
    return _DOCUSEAL_STATUS_MAP.get(str(raw).strip().lower(), STATUS_OUT_FOR_SIGNATURE)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for e-signature provider errors."""


class ProviderUnavailable(ProviderError):
    """The configured provider could not be reached or is misconfigured.

    The API layer maps this to a 502 so the user learns the e-sign backend is
    down — never a silent failure. The Manual provider never raises this (it
    needs no server)."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EsignProvider(Protocol):
    """A pluggable e-signature backend.

    ``name`` is persisted on the SignatureRequest row (so a later ``sync`` uses
    the right backend). ``is_remote`` is False for providers with no external
    system to poll (the Manual fallback) — the service skips ``get_status`` for
    those (they advance only via an explicit re-upload)."""

    name: str
    is_remote: bool

    async def create_request(
        self,
        document_bytes: bytes,
        signer_name: str,
        signer_email: str | None,
        subject: str | None,
    ) -> dict: ...

    async def get_status(self, external_id: str) -> str: ...

    async def fetch_signed(self, external_id: str) -> bytes | None: ...


# ---------------------------------------------------------------------------
# Manual provider — always available, no external server
# ---------------------------------------------------------------------------


class ManualProvider:
    """The always-available fallback. Needs NO network.

    ``create_request`` marks the request as awaiting a wet / manual signature
    and returns no signing URL. Completion happens out-of-band: the user
    downloads the work-product PDF, signs it, and re-uploads it via the
    ``mark-signed`` route (which calls ``service.record_signed``)."""

    name = "manual"
    is_remote = False

    async def create_request(
        self,
        document_bytes: bytes,
        signer_name: str,
        signer_email: str | None,
        subject: str | None,
    ) -> dict:
        # No server call of any kind. The document is ready to be downloaded,
        # signed by hand, and re-uploaded.
        return {
            "external_id": None,
            "signing_url": None,
            "status": STATUS_OUT_FOR_SIGNATURE,
        }

    async def get_status(self, external_id: str) -> str:
        # There is no remote system to ask. The manual workflow advances only
        # through record_signed (re-upload) or cancel.
        return STATUS_OUT_FOR_SIGNATURE

    async def fetch_signed(self, external_id: str) -> bytes | None:
        # The signed PDF is uploaded by the user, not fetched from anywhere.
        return None


# ---------------------------------------------------------------------------
# Docuseal provider — self-hostable OSS e-signature
# ---------------------------------------------------------------------------

# Default per-call timeout. Docuseal is typically on the same network in
# production; this ceiling keeps a stuck server from hanging a request worker.
_DOCUSEAL_TIMEOUT_S = 20.0


class DocusealProvider:
    """A real provider backed by a Docuseal REST API.

    Flow (all over ``httpx`` with timeouts; every failure -> ProviderUnavailable):

      create_request -> POST /templates/pdf  (create a one-off template from the
                        filled PDF, base64-encoded)
                     -> POST /submissions     (open a submission for one signer)
                     -> returns the submission id + the signer's signing URL.
      get_status     -> GET  /submissions/{id}  (map Docuseal status -> vocab)
      fetch_signed   -> GET  /submissions/{id}  (find the completed PDF url) ->
                        GET that url -> bytes, only once completed.

    NOTE: implemented to Docuseal's documented REST shape but UNVERIFIED against
    a live server in this build (no Docuseal instance available here). The
    parsing is deliberately tolerant of minor response-shape variations."""

    name = "docuseal"
    is_remote = True

    def __init__(
        self, base_url: str, api_key: str, *, timeout_s: float = _DOCUSEAL_TIMEOUT_S
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout_s

    # -- HTTP helpers -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # Docuseal authenticates with the X-Auth-Token header.
        return {"X-Auth-Token": self._key, "Content-Type": "application/json"}

    async def _request_json(self, method: str, path: str, payload: dict | None = None):
        if not self._base or not self._key:
            raise ProviderUnavailable(
                "Docuseal is not configured (ESIGN_DOCUSEAL_BASE_URL / "
                "ESIGN_DOCUSEAL_API_KEY)."
            )
        url = f"{self._base}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(
                    method, url, headers=self._headers(), json=payload
                )
        except httpx.HTTPError as exc:  # network/timeout — never leak internals
            raise ProviderUnavailable(
                f"Docuseal unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code >= 400:
            raise ProviderUnavailable(
                f"Docuseal returned HTTP {resp.status_code} for {method} {path}."
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderUnavailable("Docuseal returned a non-JSON response.") from exc

    async def _download(self, url: str) -> bytes:
        # Completed-document URLs may be absolute (e.g. an object-store link) or
        # relative to the Docuseal base. Only send the auth header for our base.
        absolute = url.startswith("http://") or url.startswith("https://")
        full = url if absolute else f"{self._base}{url}"
        headers = None if absolute else {"X-Auth-Token": self._key}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(full, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"Docuseal document unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code >= 400:
            raise ProviderUnavailable(
                f"Docuseal document fetch failed (HTTP {resp.status_code})."
            )
        return resp.content

    # -- parsing helpers ----------------------------------------------------

    def _parse_submission_create(self, body) -> tuple[str | None, str | None, str]:
        """Extract (external_id, signing_url, status) from a create response.

        Docuseal's POST /submissions returns an array of submitter objects (one
        per signer), each carrying a ``submission_id``, a ``slug`` (the signing
        link is ``{base}/s/{slug}``), and a ``status``. Tolerant of the response
        being a bare dict instead of a list."""
        submitters = body if isinstance(body, list) else (
            body.get("submitters") if isinstance(body, dict) else None
        ) or ([body] if isinstance(body, dict) else [])
        first = submitters[0] if submitters else {}
        submission_id = (
            first.get("submission_id")
            or (body.get("id") if isinstance(body, dict) else None)
        )
        slug = first.get("slug") or first.get("embed_src")
        signing_url = None
        if slug:
            signing_url = slug if str(slug).startswith("http") else f"{self._base}/s/{slug}"
        status = normalize_docuseal_status(first.get("status"))
        # A freshly created submission is at least 'sent' (a link exists).
        if status == STATUS_OUT_FOR_SIGNATURE and signing_url:
            status = STATUS_SENT
        return (str(submission_id) if submission_id is not None else None, signing_url, status)

    def _submission_status(self, body) -> str:
        if not isinstance(body, dict):
            return STATUS_OUT_FOR_SIGNATURE
        # A submission is complete if it carries completed_at or status completed.
        if body.get("completed_at"):
            return STATUS_SIGNED
        top = body.get("status")
        if top:
            mapped = normalize_docuseal_status(top)
            if mapped in (STATUS_SIGNED, STATUS_DECLINED):
                return mapped
        # Otherwise derive from the submitters: any declined -> declined; all
        # completed -> signed; any opened -> out_for_signature; else sent.
        submitters = body.get("submitters") or []
        states = [normalize_docuseal_status(s.get("status")) for s in submitters]
        if STATUS_DECLINED in states:
            return STATUS_DECLINED
        if states and all(s == STATUS_SIGNED for s in states):
            return STATUS_SIGNED
        if STATUS_OUT_FOR_SIGNATURE in states:
            return STATUS_OUT_FOR_SIGNATURE
        return STATUS_SENT

    def _completed_document_url(self, body) -> str | None:
        if not isinstance(body, dict):
            return None
        if body.get("combined_document_url"):
            return body["combined_document_url"]
        docs = body.get("documents") or []
        for doc in docs:
            url = doc.get("url") if isinstance(doc, dict) else None
            if url:
                return url
        return None

    # -- EsignProvider interface -------------------------------------------

    async def create_request(
        self,
        document_bytes: bytes,
        signer_name: str,
        signer_email: str | None,
        subject: str | None,
    ) -> dict:
        encoded = base64.b64encode(document_bytes).decode("ascii")
        template = await self._request_json(
            "POST",
            "/templates/pdf",
            {
                "name": subject or "Document for signature",
                "documents": [{"name": "document", "file": encoded}],
            },
        )
        template_id = template.get("id") if isinstance(template, dict) else None
        if not template_id:
            raise ProviderUnavailable(
                "Docuseal did not return a template id for the uploaded PDF."
            )
        submitter: dict[str, object] = {"role": "First Party"}
        if signer_name:
            submitter["name"] = signer_name
        if signer_email:
            submitter["email"] = signer_email
        submission = await self._request_json(
            "POST",
            "/submissions",
            {
                "template_id": template_id,
                # Only ask Docuseal to email the signer when we have an address.
                "send_email": bool(signer_email),
                "submitters": [submitter],
            },
        )
        external_id, signing_url, status = self._parse_submission_create(submission)
        return {
            "external_id": external_id,
            "signing_url": signing_url,
            "status": status,
        }

    async def get_status(self, external_id: str) -> str:
        body = await self._request_json("GET", f"/submissions/{external_id}")
        return self._submission_status(body)

    async def fetch_signed(self, external_id: str) -> bytes | None:
        body = await self._request_json("GET", f"/submissions/{external_id}")
        if self._submission_status(body) != STATUS_SIGNED:
            return None
        url = self._completed_document_url(body)
        if not url:
            return None
        return await self._download(url)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

ENV_DOCUSEAL_BASE_URL = "ESIGN_DOCUSEAL_BASE_URL"
ENV_DOCUSEAL_API_KEY = "ESIGN_DOCUSEAL_API_KEY"


def resolve_provider(env: Mapping[str, str] | None = None) -> EsignProvider:
    """Return a Docuseal provider when configured, else the Manual fallback.

    ``env`` defaults to ``os.environ`` (read at call time so deploy-time env or
    a test override takes effect). Docuseal is selected ONLY when BOTH
    ``ESIGN_DOCUSEAL_BASE_URL`` and ``ESIGN_DOCUSEAL_API_KEY`` are non-empty;
    a partial configuration falls back to Manual (never a half-wired remote)."""
    source = env if env is not None else os.environ
    base = (source.get(ENV_DOCUSEAL_BASE_URL, "") or "").strip()
    key = (source.get(ENV_DOCUSEAL_API_KEY, "") or "").strip()
    if base and key:
        return DocusealProvider(base, key)
    return ManualProvider()


__all__ = (
    "EsignProvider",
    "DocusealProvider",
    "ManualProvider",
    "ProviderError",
    "ProviderUnavailable",
    "resolve_provider",
    "coerce_status",
    "normalize_docuseal_status",
    "SIGNATURE_STATUSES",
    "TERMINAL_STATUSES",
    "STATUS_CREATED",
    "STATUS_SENT",
    "STATUS_OUT_FOR_SIGNATURE",
    "STATUS_SIGNED",
    "STATUS_DECLINED",
    "STATUS_CANCELED",
    "ENV_DOCUSEAL_BASE_URL",
    "ENV_DOCUSEAL_API_KEY",
)

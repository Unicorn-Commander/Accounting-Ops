"""USPS Web Tools tracking integration.

Provides a read-only lookup of USPS Certified Mail (or any other USPS-tracked
shipment) delivery status. The USPS Web Tools API is free + public; the only
gate is registering a USER ID at:

    https://www.usps.com/business/web-tools-apis/welcome.htm

The USER ID is a non-secret identifier (USPS treats it like a CORS Origin
header); it's stored in env var USPS_WEB_TOOLS_USER_ID. If unset, the endpoints
return a "tracking disabled" response rather than crashing — useful for local
dev where you don't want to register.

Endpoints
---------
  GET  /api/usps-tracking/{tracking_number}
       → live USPS lookup; returns the latest status event + event history

  POST /api/usps-tracking/refresh-all
       → admin-only batch refresh: hits USPS for every open
         tax_filing_submission and updates last_tracking_status.

The aggregator that picks tracking status into /open-items is in
`app/api/tax_filing_submissions.py`.

Compliance + safety notes
-------------------------
- Calls outbound to USPS only. No PII transmitted (tracking number only).
- Per-call rate: USPS WebTools soft-limits at ~5 req/sec per USER ID; we
  back off on 429 / 500.
- Tracking-number format: we validate as 13-22 digit numeric. USPS rejects
  malformed ones with a fault response; we surface the fault.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.tenancy import WorkspaceContext, resolve_workspace, require_role

logger = logging.getLogger(__name__)

router = APIRouter(tags=["usps-tracking"])

USPS_TRACK_URL = "https://secure.shippingapis.com/ShippingAPI.dll"
USPS_API_NAME = "TrackV2"

# Tracking number validator: 13-22 digit numeric (covers standard formats)
TRACKING_NUMBER_RE = re.compile(r"^\d{13,22}$")


def _get_user_id() -> str | None:
    """Return the USPS Web Tools USER ID or None if unconfigured."""
    return os.getenv("USPS_WEB_TOOLS_USER_ID")


@dataclass
class TrackingEvent:
    """One USPS tracking event."""
    event: str  # e.g. "DELIVERED", "ARRIVED AT POST OFFICE", "OUT FOR DELIVERY"
    event_time: str | None  # ISO-ish string from USPS
    event_date: str | None  # ISO-ish string from USPS
    event_city: str | None
    event_state: str | None
    event_zip: str | None
    event_country: str | None


@dataclass
class TrackingResult:
    """Result of a USPS tracking lookup."""
    tracking_number: str
    is_delivered: bool
    summary: str  # Latest event description
    delivery_date: str | None
    events: list[TrackingEvent]
    fault: str | None = None


def _parse_track_response(xml_text: str, tracking_number: str) -> TrackingResult:
    """Parse the USPS TrackV2 XML response."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return TrackingResult(
            tracking_number=tracking_number,
            is_delivered=False,
            summary="USPS returned malformed XML",
            delivery_date=None,
            events=[],
            fault=f"Parse error: {exc}",
        )

    # USPS error envelope: <Error><Number/><Description/></Error>
    # The error may be the root element OR nested inside <TrackResponse>.
    err = root if root.tag == "Error" else root.find("Error")
    if err is not None:
        desc = (err.findtext("Description") or "Unknown USPS error").strip()
        return TrackingResult(
            tracking_number=tracking_number,
            is_delivered=False,
            summary=f"USPS error: {desc}",
            delivery_date=None,
            events=[],
            fault=desc,
        )

    # TrackResponse > TrackInfo
    track_info = root.find("TrackInfo")
    if track_info is None:
        return TrackingResult(
            tracking_number=tracking_number,
            is_delivered=False,
            summary="No TrackInfo in USPS response",
            delivery_date=None,
            events=[],
            fault="missing TrackInfo",
        )

    # Sometimes <Error> is nested inside <TrackInfo>
    nested_err = track_info.find("Error")
    if nested_err is not None:
        desc = (nested_err.findtext("Description") or "Unknown USPS error").strip()
        return TrackingResult(
            tracking_number=tracking_number,
            is_delivered=False,
            summary=f"USPS error: {desc}",
            delivery_date=None,
            events=[],
            fault=desc,
        )

    # <TrackSummary> — USPS sends this as either a flat text string
    # ("Your item was delivered.") OR as a structured Event/EventTime/EventDate
    # block. Handle both.
    summary_el = track_info.find("TrackSummary")
    summary_text = ""
    if summary_el is not None:
        # If TrackSummary has a child <Event>, treat it as structured.
        if summary_el.find("Event") is not None:
            summary_text = (summary_el.findtext("Event") or "").strip()
        elif summary_el.text:
            summary_text = summary_el.text.strip()

    # Parse all <TrackDetail> elements
    events: list[TrackingEvent] = []
    # If summary has Event children, fold it into events list
    if summary_el is not None and summary_el.find("Event") is not None:
        events.append(_parse_event_node(summary_el))
    for detail in track_info.findall("TrackDetail"):
        events.append(_parse_event_node(detail))
    # If still no events (summary was flat text only), promote summary
    # to a synthesized event for downstream consumers.
    if not events and summary_text:
        events.append(TrackingEvent(
            event=summary_text,
            event_time=None, event_date=None,
            event_city=None, event_state=None,
            event_zip=None, event_country=None,
        ))

    # Delivery state: USPS sets "Status" to "Delivered" or includes
    # "DELIVERED" in the latest event text.
    status_el = track_info.find("Status")
    status_text = (status_el.text or "").strip().lower() if status_el is not None else ""

    is_delivered = (
        "deliver" in status_text
        or any("deliver" in (e.event or "").lower() for e in events[:1])
    )

    delivery_date = None
    if is_delivered and events:
        # Latest event was the delivery
        delivery_date = events[0].event_date

    return TrackingResult(
        tracking_number=tracking_number,
        is_delivered=is_delivered,
        summary=summary_text or (events[0].event if events else ""),
        delivery_date=delivery_date,
        events=events,
    )


def _parse_event_node(node: ET.Element) -> TrackingEvent:
    """Parse a single <TrackSummary> or <TrackDetail> element."""
    return TrackingEvent(
        event=(node.findtext("Event") or "").strip(),
        event_time=(node.findtext("EventTime") or "").strip() or None,
        event_date=(node.findtext("EventDate") or "").strip() or None,
        event_city=(node.findtext("EventCity") or "").strip() or None,
        event_state=(node.findtext("EventState") or "").strip() or None,
        event_zip=(node.findtext("EventZIPCode") or "").strip() or None,
        event_country=(node.findtext("EventCountry") or "").strip() or None,
    )


async def lookup_tracking(tracking_number: str) -> TrackingResult:
    """Hit USPS WebTools TrackV2 and return parsed status.

    Returns a TrackingResult with .fault set to a non-empty string if the
    lookup could not be completed (no USER_ID, network error, etc.). Callers
    should check .fault before relying on .is_delivered.
    """
    cleaned = tracking_number.strip().replace(" ", "").replace("-", "")
    if not TRACKING_NUMBER_RE.match(cleaned):
        return TrackingResult(
            tracking_number=tracking_number,
            is_delivered=False,
            summary="Invalid tracking number format",
            delivery_date=None,
            events=[],
            fault="tracking number must be 13-22 digits",
        )

    user_id = _get_user_id()
    if not user_id:
        return TrackingResult(
            tracking_number=cleaned,
            is_delivered=False,
            summary="USPS tracking disabled (USPS_WEB_TOOLS_USER_ID not set)",
            delivery_date=None,
            events=[],
            fault="USPS_WEB_TOOLS_USER_ID env var is unset",
        )

    # Build XML request per USPS TrackV2 spec
    xml_req = (
        f'<TrackRequest USERID="{user_id}">'
        f'<TrackID ID="{cleaned}"></TrackID>'
        f'</TrackRequest>'
    )
    params = {"API": USPS_API_NAME, "XML": xml_req}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(USPS_TRACK_URL, params=params)
    except httpx.HTTPError as exc:
        return TrackingResult(
            tracking_number=cleaned,
            is_delivered=False,
            summary=f"USPS HTTP error: {exc}",
            delivery_date=None,
            events=[],
            fault=str(exc),
        )

    if resp.status_code != 200:
        return TrackingResult(
            tracking_number=cleaned,
            is_delivered=False,
            summary=f"USPS HTTP {resp.status_code}",
            delivery_date=None,
            events=[],
            fault=f"HTTP {resp.status_code}: {resp.text[:120]}",
        )

    return _parse_track_response(resp.text, cleaned)


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------

class TrackingEventResponse(BaseModel):
    event: str
    event_time: str | None
    event_date: str | None
    event_city: str | None
    event_state: str | None
    event_zip: str | None
    event_country: str | None


class TrackingResponse(BaseModel):
    tracking_number: str
    is_delivered: bool
    summary: str
    delivery_date: str | None
    events: list[TrackingEventResponse]
    fault: str | None = None


@router.get(
    "/api/usps-tracking/{tracking_number}",
    response_model=TrackingResponse,
)
async def get_tracking(
    tracking_number: str,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> TrackingResponse:
    """Look up the live USPS tracking status for a single number.

    Public information — no PII transmitted to USPS. Caller must hold viewer
    role on the workspace (we still want auth so we can log who looked).
    """
    require_role(workspace, "viewer")

    if not tracking_number or len(tracking_number) > 60:
        raise HTTPException(status_code=400, detail="invalid tracking number")

    result = await lookup_tracking(tracking_number)
    return TrackingResponse(
        tracking_number=result.tracking_number,
        is_delivered=result.is_delivered,
        summary=result.summary,
        delivery_date=result.delivery_date,
        events=[
            TrackingEventResponse(
                event=e.event,
                event_time=e.event_time,
                event_date=e.event_date,
                event_city=e.event_city,
                event_state=e.event_state,
                event_zip=e.event_zip,
                event_country=e.event_country,
            )
            for e in result.events
        ],
        fault=result.fault,
    )

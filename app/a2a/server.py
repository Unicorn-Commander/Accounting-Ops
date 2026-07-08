import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.a2a.agent_card import accounting_ops_agent_card

router = APIRouter(prefix="/a2a", tags=["a2a"])


@router.get("/agent-card.json")
async def agent_card() -> dict:
    return accounting_ops_agent_card()


@router.post("")
async def json_rpc_placeholder(payload: dict) -> dict:
    method = payload.get("method")
    if method == "agent/card":
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": accounting_ops_agent_card()}
    return {
        "jsonrpc": "2.0",
        "id": payload.get("id"),
        "error": {"code": -32000, "message": "Phase 1"},
    }


@router.get("/stream")
async def event_stream():
    async def events():
        yield f"data: {json.dumps({'status': 'connected', 'service': 'Accounting-Ops'})}\n\n"
        await asyncio.sleep(0.01)

    return StreamingResponse(events(), media_type="text/event-stream")

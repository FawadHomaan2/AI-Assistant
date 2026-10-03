"""HTTP and WebSocket endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from jarvis.config import secrets
from jarvis.transport import auth
from jarvis.transport.schemas import (
    ChatRequest,
    HealthOut,
    ProviderOut,
    SessionOut,
    TurnOut,
)
from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger

log = get_logger(__name__)
router = APIRouter()


def _ctx(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.ctx


@router.get("/health", response_model=HealthOut)
async def health(request: Request) -> HealthOut:
    ctx = _ctx(request)
    store = secrets.status()
    return HealthOut(
        status="ok",
        version=ctx.version,
        schema_version=ctx.db.version,
        mode=ctx.settings.mode,
        emergency_stop=ctx.estop.engaged,
        default_provider=ctx.settings.ai.default,
        credential_store={
            "available": store.available,
            "backend": store.backend,
            "detail": store.detail,
        },
    )


@router.get("/providers", response_model=list[ProviderOut])
async def providers(request: Request) -> list[ProviderOut]:
    caps = _ctx(request).gateway.capabilities()
    return [ProviderOut(**vars(c)) for c in caps]


@router.get("/providers/health")
async def provider_health(request: Request) -> dict[str, dict[str, object]]:
    results = await _ctx(request).gateway.health()
    return {name: {"ok": ok, "detail": detail} for name, (ok, detail) in results.items()}


@router.post("/sessions", response_model=SessionOut)
async def create_session(request: Request) -> SessionOut:
    ctx = _ctx(request)
    session = ctx.sessions.create(mode=ctx.settings.mode)
    return SessionOut(**vars(session))


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(request: Request, limit: int = 50) -> list[SessionOut]:
    return [SessionOut(**vars(s)) for s in _ctx(request).sessions.list_recent(limit)]


@router.get("/sessions/{session_id}/turns", response_model=list[TurnOut])
async def session_turns(request: Request, session_id: str, limit: int = 200) -> list[TurnOut]:
    ctx = _ctx(request)
    if ctx.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail=f"No session {session_id!r}")
    return [
        TurnOut(
            id=t.id,
            role=t.role,
            content=t.content,
            created_at=t.created_at,
            provider=t.provider,
            model=t.model,
            tokens_in=t.tokens_in,
            tokens_out=t.tokens_out,
            latency_ms=t.latency_ms,
            error_code=t.error_code,
        )
        for t in ctx.turns.history(session_id, limit)
    ]


@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str) -> dict[str, bool]:
    _ctx(request).sessions.delete(session_id)
    return {"deleted": True}


@router.delete("/sessions")
async def delete_all_sessions(request: Request) -> dict[str, int]:
    """Privacy dashboard: clear conversation history."""
    count = _ctx(request).sessions.delete_all()
    log.info("conversation history cleared", sessions_deleted=count)
    return {"deleted": count}


@router.get("/audit")
async def audit_log(request: Request, limit: int = 100) -> dict[str, object]:
    ctx = _ctx(request)
    intact, broken_at = ctx.audit.verify()
    return {
        "entries": ctx.audit.recent(limit),
        "chain_intact": intact,
        "first_broken_id": broken_at,
    }


@router.post("/emergency-stop")
async def emergency_stop(request: Request) -> dict[str, bool]:
    _ctx(request).estop.engage("requested via API")
    return {"engaged": True}


@router.delete("/emergency-stop")
async def clear_emergency_stop(request: Request) -> dict[str, bool]:
    _ctx(request).estop.clear()
    return {"engaged": False}


@router.post("/chat")
async def chat(request: Request, body: ChatRequest) -> dict[str, object]:
    """Non-streaming chat. The UI uses the WebSocket; this is for scripts and tests."""
    ctx = _ctx(request)
    session_id = body.session_id or ctx.sessions.create(mode=ctx.settings.mode).id
    if ctx.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail=f"No session {session_id!r}")

    events: list[dict[str, object]] = []
    text_parts: list[str] = []
    async for event in ctx.orchestrator.handle(session_id, body.message):
        payload = event.to_json()
        events.append(payload)
        if payload["type"] == "delta":
            text_parts.append(str(payload.get("text", "")))
    return {"session_id": session_id, "text": "".join(text_parts), "events": events}


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    """Streaming channel.

    Client sends {"type":"chat","message":...,"session_id":...}; the server
    replies with a stream of agent events, each a JSON object with a `type`.
    """
    ctx = ws.app.state.ctx
    if not await auth.authorise_websocket(ws, ctx.token):
        return
    await ws.accept()
    log.info("websocket connected")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json(
                    {"type": "error", "code": "jarvis.bad_request", "message": "Malformed JSON."}
                )
                continue

            kind = msg.get("type")
            if kind == "ping":
                await ws.send_json({"type": "pong"})
                continue
            if kind != "chat":
                await ws.send_json(
                    {
                        "type": "error",
                        "code": "jarvis.bad_request",
                        "message": f"Unknown message type {kind!r}.",
                    }
                )
                continue

            text = str(msg.get("message", ""))
            session_id = msg.get("session_id") or ctx.sessions.create(mode=ctx.settings.mode).id
            if ctx.sessions.get(session_id) is None:
                session_id = ctx.sessions.create(mode=ctx.settings.mode).id

            try:
                async for event in ctx.orchestrator.handle(session_id, text):
                    payload = event.to_json()
                    payload["session_id"] = session_id
                    await ws.send_json(payload)
            except JarvisError as exc:
                await ws.send_json({"type": "error", **exc.to_dict()})
            except Exception as exc:
                log.exception("unhandled error during turn")
                await ws.send_json(
                    {
                        "type": "error",
                        "code": "jarvis.internal",
                        "message": f"Internal error: {exc}",
                    }
                )
    except WebSocketDisconnect:
        log.info("websocket disconnected")

"""HTTP and WebSocket endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from jarvis.browser.session import BrowserSession
from jarvis.config import secrets
from jarvis.db.repositories import AuditEntry, AuditRepository
from jarvis.governance.consent import ConsentAnswer
from jarvis.governance.risk import MODE_AUTO_CEILING
from jarvis.governance.scopes import PATH_SCOPES, Scope
from jarvis.tools.documents import DocumentTool
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


@router.get("/tools")
async def tools(request: Request) -> list[dict[str, object]]:
    specs: list[dict[str, object]] = _ctx(request).registry.specs()
    return specs


@router.get("/permissions")
async def permissions(request: Request) -> dict[str, object]:
    ctx = _ctx(request)
    return {"policy": ctx.policy.describe(), "paths": ctx.jail.describe()}


@router.post("/permissions/scopes/{scope}")
async def grant_scope(request: Request, scope: str) -> dict[str, object]:
    """Grant a capability, optionally to one folder and for a limited time."""
    ctx = _ctx(request)
    body = await request.json() if await request.body() else {}
    try:
        parsed = Scope(scope)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown scope {scope!r}") from exc

    target = str(body.get("target") or "")
    ttl = body.get("ttlMinutes")
    if target and parsed not in PATH_SCOPES:
        raise HTTPException(
            status_code=422,
            detail=f"{scope} is not a folder permission, so a target makes no sense for it.",
        )
    grant = ctx.policy.grants.grant(parsed, target, ttl_minutes=int(ttl) if ttl else None)
    ctx.grants.save(ctx.policy.grants)
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="permission.grant",
            args_digest=AuditRepository.digest({"scope": scope, "target": target, "ttl": ttl}),
            decision="allowed",
        )
    )
    log.info("scope granted", scope=scope, target=target, expires=grant.expires_at)
    return {"granted": True, "grant": grant.to_dict(), "scopes": ctx.policy.grants.describe()}


@router.delete("/permissions/scopes/{scope}")
async def revoke_scope(request: Request, scope: str, target: str = "") -> dict[str, object]:
    ctx = _ctx(request)
    try:
        removed = ctx.policy.grants.revoke(Scope(scope), target)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown scope {scope!r}") from exc
    ctx.grants.save(ctx.policy.grants)
    # A revoked capability must also drop any "allow this for the session"
    # approval that referenced it, or the grant comes back by the side door.
    ctx.policy.forget_all()
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="permission.revoke",
            args_digest=AuditRepository.digest({"scope": scope, "target": target}),
            decision="allowed",
        )
    )
    log.info("scope revoked", scope=scope, target=target, removed=removed)
    return {"granted": False, "removed": removed, "scopes": ctx.policy.grants.describe()}


@router.post("/permissions/mode")
async def set_mode(request: Request) -> dict[str, object]:
    """Change the global posture, and persist it.

    Persisted deliberately: a paused assistant that quietly returns to acting
    after a restart is the worst possible default, because the user's last
    instruction was "stop".
    """
    ctx = _ctx(request)
    body = await request.json() if await request.body() else {}
    mode = str(body.get("mode") or ctx.policy.mode)
    if mode not in MODE_AUTO_CEILING:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown mode {mode!r}. Expected one of {sorted(MODE_AUTO_CEILING)}.",
        )
    read_only = bool(body.get("readOnly", ctx.policy.read_only))
    ctx.policy.mode = mode
    ctx.policy.read_only = read_only
    # Approvals were given under the old posture; they do not carry over.
    ctx.policy.forget_all()
    ctx.grants.save_posture(mode, read_only)
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="permission.mode",
            args_digest=AuditRepository.digest({"mode": mode, "readOnly": read_only}),
            decision="allowed",
        )
    )
    return {"policy": ctx.policy.describe()}


@router.delete("/permissions/remembered")
async def forget_approvals(request: Request) -> dict[str, object]:
    """Drop every "allow this for the session" approval."""
    ctx = _ctx(request)
    ctx.policy.forget_all()
    return {"policy": ctx.policy.describe()}


@router.get("/voice/status")
async def voice_status(request: Request) -> dict[str, object]:
    ctx = _ctx(request)
    status = ctx.voice.status()
    return {
        **status.to_dict(),
        "state": ctx.voice.state.value,
        "reason": ctx.voice.unavailable_reason(),
        "settings": {
            "enabled": ctx.settings.voice.enabled,
            "wakeWord": ctx.settings.voice.wake_word,
            "pushToTalk": ctx.settings.voice.push_to_talk,
        },
    }


@router.get("/browser/status")
async def browser_status(request: Request) -> dict[str, object]:
    """What the browser may reach, so the interface can show it rather than
    describe an intended default that may not match the config file."""
    ctx = _ctx(request)
    settings = ctx.browser.settings
    available, detail = BrowserSession.availability(settings.executable_path)
    return {
        "available": available,
        "detail": detail,
        "running": bool(await ctx.browser.current_url()),
        "currentUrl": await ctx.browser.current_url(),
        "allowedHosts": sorted(settings.allowed_hosts),
        "allowAnyHost": settings.allow_any_host,
        "allowLoopback": settings.allow_loopback,
        "headless": settings.headless,
        "searchEngine": ctx.settings.browser.search_engine,
        "ownProfile": True,
    }


@router.get("/memory")
async def memory_list(
    request: Request, tier: str = "", status: str = "", q: str = "", limit: int = 200
) -> dict[str, object]:
    """Everything Jarvis has learned, listed and searchable.

    Candidates are included deliberately. Seeing what Jarvis is *about* to
    believe, before it acts on it, is the point of the dashboard.
    """
    ctx = _ctx(request)
    store = ctx.memory
    if q:
        found = store.search(q, limit=limit, include_candidates=True)
    else:
        found = store.list_all(tier=tier or None, status=status or None, limit=limit)
    return {"memories": [m.to_dict() for m in found], "stats": store.stats()}


@router.patch("/memory/{memory_id}")
async def memory_update(request: Request, memory_id: str) -> dict[str, object]:
    """Correct a memory. An edit counts as you stating it, so it becomes active."""
    ctx = _ctx(request)
    body = await request.json()
    if "value" in body:
        memory = ctx.memory.set_value(memory_id, body["value"])
    elif "pinned" in body:
        memory = ctx.memory.pin(memory_id, bool(body["pinned"]))
    else:
        raise HTTPException(status_code=422, detail="Send either `value` or `pinned`.")
    if memory is None:
        raise HTTPException(status_code=404, detail=f"No memory with id {memory_id!r}.")
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="memory.edit",
            args_digest=AuditRepository.digest({"id": memory_id}),
            decision="allowed",
        )
    )
    result: dict[str, object] = memory.to_dict()
    return result


@router.delete("/memory/{memory_id}")
async def memory_delete(request: Request, memory_id: str) -> dict[str, object]:
    ctx = _ctx(request)
    removed = ctx.memory.forget(memory_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"No memory with id {memory_id!r}.")
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="memory.forget",
            args_digest=AuditRepository.digest({"id": memory_id}),
            decision="allowed",
        )
    )
    return {"deleted": memory_id}


@router.delete("/memory")
async def memory_clear(request: Request, tier: str = "") -> dict[str, object]:
    """Delete everything, or one tier. Gone means gone — there is no archive."""
    ctx = _ctx(request)
    removed = ctx.memory.clear(tier or None)
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="memory.clear",
            args_digest=AuditRepository.digest({"tier": tier, "removed": removed}),
            decision="allowed",
        )
    )
    return {"deleted": removed, "tier": tier or "all"}


@router.get("/security/status")
async def security_status(request: Request) -> dict[str, object]:
    """Posture without rescanning, for the panel's first paint."""
    status: dict[str, object] = _ctx(request).security.status()
    return status


@router.post("/security/scan")
async def security_scan(request: Request) -> dict[str, object]:
    ctx = _ctx(request)
    report = ctx.security.scan()
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="security.scan",
            args_digest=AuditRepository.digest(
                {"checks": report.to_dict()["checksTotal"], "findings": len(report.actionable)}
            ),
            decision="allowed",
        )
    )
    body: dict[str, object] = report.to_dict()
    return body


@router.get("/security/findings")
async def security_findings(request: Request) -> dict[str, object]:
    return {"findings": _ctx(request).security.findings.open_findings()}


@router.post("/security/findings/{finding_id}/acknowledge")
async def security_acknowledge(request: Request, finding_id: str) -> dict[str, object]:
    ctx = _ctx(request)
    if not ctx.security.findings.acknowledge(finding_id):
        raise HTTPException(status_code=404, detail=f"No finding with id {finding_id!r}.")
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="security.acknowledge",
            args_digest=AuditRepository.digest({"id": finding_id}),
            decision="allowed",
        )
    )
    return {"acknowledged": finding_id}


@router.get("/security/baseline")
async def security_baseline(request: Request, category: str = "") -> dict[str, object]:
    entries = _ctx(request).security.baseline.entries(category)
    return {"baseline": [e.to_dict() for e in entries]}


@router.post("/security/baseline/{print_}/trust")
async def security_trust(request: Request, print_: str) -> dict[str, object]:
    """Stop reporting something. Trusting it does not make it safe."""
    ctx = _ctx(request)
    body = await request.json() if await request.body() else {}
    trusted = bool(body.get("trusted", True))
    if not ctx.security.baseline.trust(print_, trusted):
        raise HTTPException(status_code=404, detail=f"Nothing in the baseline matches {print_!r}.")
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="security.trust",
            args_digest=AuditRepository.digest({"fingerprint": print_, "trusted": trusted}),
            decision="allowed",
        )
    )
    return {"fingerprint": print_, "trusted": trusted}


@router.get("/documents/formats")
async def document_formats(request: Request) -> list[dict[str, object]]:
    del request
    formats: list[dict[str, object]] = DocumentTool.formats()
    return formats


@router.post("/emergency-stop")
async def emergency_stop(request: Request) -> dict[str, object]:
    ctx = _ctx(request)
    ctx.estop.engage("requested via API")
    # Anything waiting on a confirmation is declined, so no action stays armed.
    cancelled = ctx.consent.cancel_all("emergency stop")
    return {"engaged": True, "cancelledPrompts": cancelled}


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

    async def show_consent(req: object) -> None:
        """Push a confirmation prompt to this UI and let the broker wait."""
        await ws.send_json({"type": "consent.request", **req.to_dict()})  # type: ignore[attr-defined]

    ctx.consent.set_prompt(show_consent)

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
            if kind == "consent.response":
                delivered = ctx.consent.resolve(
                    str(msg.get("id", "")),
                    ConsentAnswer(
                        approved=bool(msg.get("approved")),
                        remember=str(msg.get("remember", "no")),  # type: ignore[arg-type]
                    ),
                )
                if not delivered:
                    log.warning("consent answer had nothing waiting", consent_id=msg.get("id"))
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

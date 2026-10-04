"""HTTP and WebSocket endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from jarvis.config import models, secrets, writer
from jarvis.config.settings import PRESETS
from jarvis.db.repositories import AuditEntry, AuditRepository
from jarvis.governance.consent import ConsentAnswer
from jarvis.governance.risk import MODE_AUTO_CEILING
from jarvis.governance.scopes import PATH_SCOPES, Scope
from jarvis.plugins.manifest import ManifestInvalid
from jarvis.tools.documents import DocumentTool
from jarvis.tools.plugin_proxy import register_plugins
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


async def _apply(ctx, new_settings) -> None:  # type: ignore[no-untyped-def]
    """Adopt a changed configuration without a restart.

    Both halves matter. Replacing `ctx.settings` is what later requests read;
    reloading the gateway is what drops providers already built, each of which
    captured its API key when its HTTP client was created. Without the second,
    a key stored a moment ago would not be used until the next launch.
    """
    ctx.settings = new_settings
    await ctx.gateway.reload(new_settings)


@router.get("/providers/presets")
async def provider_presets() -> dict[str, object]:
    """The services the Settings panel can set up, and where to get a key.

    Served from the core so adding one is a single entry in
    `jarvis.config.settings.PRESETS` rather than an edit in three places.
    """
    return {
        "presets": [{**preset.model_dump(), "needs_key": preset.needs_key} for preset in PRESETS]
    }


@router.post("/providers")
async def add_provider(request: Request) -> dict[str, object]:
    """Add or replace a provider, optionally storing its key in one step.

    This is the endpoint that made the cloud models reachable at all: the
    shipped `config.toml` has them commented out, so before this a fresh
    install listed `dev_echo` and the adapters' "add a key in Settings" pointed
    at a control that did not exist.
    """
    ctx = _ctx(request)
    body = await request.json() if await request.body() else {}

    name = str(body.get("name") or "").strip()
    kind = str(body.get("kind") or "").strip()
    if not kind:
        raise HTTPException(status_code=422, detail="A provider needs a `kind`.")

    key = body.get("key")
    credential = str(body.get("credential") or "").strip()
    if key and not credential:
        raise HTTPException(
            status_code=422,
            detail="A key was supplied with no `credential` name to store it under.",
        )

    try:
        updated = writer.set_provider(
            name,
            kind=kind,
            model=str(body.get("model") or "").strip(),
            base_url=str(body.get("base_url") or "").strip(),
            credential=credential,
        )
    except JarvisError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    # The config is written first on purpose. Storing the key against a
    # provider that then fails to validate would leave a secret in the keychain
    # for something that does not exist.
    if key:
        _store_key(ctx, credential, str(key), provider=name)

    await _apply(ctx, updated)
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="provider.add",
            args_digest=AuditRepository.digest(
                {"name": name, "kind": kind, "key_supplied": bool(key)}
            ),
            decision="allowed",
        )
    )
    log.info("provider configured", provider=name, kind=kind, key_supplied=bool(key))
    return {"providers": [ProviderOut(**vars(c)).model_dump() for c in ctx.gateway.capabilities()]}


@router.delete("/providers/{name}")
async def remove_provider(
    request: Request, name: str, forget_key: bool = True
) -> dict[str, object]:
    """Remove a provider, and by default its stored key with it."""
    ctx = _ctx(request)
    _, existing = _provider_or_404(ctx, name)
    credential = existing.credential

    try:
        updated = writer.remove_provider(name)
    except JarvisError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    forgotten = False
    if forget_key and credential:
        # Best effort: a provider removed while the keychain is unavailable
        # should still be removed. The alternative leaves the config entry in
        # place because of a secret nobody can reach.
        try:
            forgotten = secrets.delete(credential)
        except JarvisError as exc:
            log.warning("could not remove stored key", provider=name, detail=exc.message)

    await _apply(ctx, updated)
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="provider.remove",
            args_digest=AuditRepository.digest({"name": name, "key_removed": forgotten}),
            decision="allowed",
        )
    )
    log.info("provider removed", provider=name, key_removed=forgotten)
    return {
        "removed": True,
        "key_removed": forgotten,
        "default": updated.ai.default,
        "providers": [ProviderOut(**vars(c)).model_dump() for c in ctx.gateway.capabilities()],
    }


@router.put("/providers/{name}/credential")
async def set_provider_credential(request: Request, name: str) -> dict[str, object]:
    """Store an API key for a configured provider.

    The key goes to the OS credential store and nowhere else: not to
    `config.toml`, not to the audit log, not to the response, and not to any
    log line. Settings can report "configured" afterwards but can never read it
    back.
    """
    ctx = _ctx(request)
    _, existing = _provider_or_404(ctx, name)
    if not existing.credential:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Provider {name!r} has no credential name, so there is nowhere to "
                "store a key. Set one first, or use a provider that needs no key."
            ),
        )

    body = await request.json() if await request.body() else {}
    key = str(body.get("key") or "")
    _store_key(ctx, existing.credential, key, provider=name)

    # Reloaded so the next request builds a client carrying the new key.
    await _apply(ctx, ctx.settings)
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="provider.credential.set",
            args_digest=AuditRepository.digest({"provider": name, "entry": existing.credential}),
            decision="allowed",
        )
    )
    return {"configured": True, "provider": name}


@router.delete("/providers/{name}/credential")
async def clear_provider_credential(request: Request, name: str) -> dict[str, object]:
    ctx = _ctx(request)
    _, existing = _provider_or_404(ctx, name)
    if not existing.credential:
        return {"configured": False, "provider": name, "removed": False}

    try:
        removed = secrets.delete(existing.credential)
    except JarvisError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    await _apply(ctx, ctx.settings)
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="provider.credential.clear",
            args_digest=AuditRepository.digest({"provider": name}),
            decision="allowed",
        )
    )
    return {"configured": False, "provider": name, "removed": removed}


@router.get("/ai")
async def ai_settings(request: Request) -> dict[str, object]:
    """Which provider is in use, and whether cloud models may be used at all.

    Separate from `/health`, which reports the default provider but not the two
    cloud flags — the panel needs both to render a switch that is not lying
    about its own position.
    """
    ctx = _ctx(request)
    store = secrets.status()
    return {
        "default": ctx.settings.ai.default,
        "allow_cloud": ctx.settings.ai.allow_cloud,
        "allow_cloud_content": ctx.settings.ai.allow_cloud_content,
        "credential_store": {
            "available": store.available,
            "backend": store.backend,
            "detail": store.detail,
        },
    }


@router.post("/ai")
async def set_ai_settings(request: Request) -> dict[str, object]:
    """Choose the provider in use, and whether cloud models may be used at all.

    `allow_cloud` is a separate decision from configuring a provider, and stays
    that way: a key stored for a rainy day must not start sending conversations
    off the machine on its own.
    """
    ctx = _ctx(request)
    body = await request.json() if await request.body() else {}

    def flag(field: str) -> bool | None:
        return None if body.get(field) is None else bool(body[field])

    default = body.get("default")
    try:
        updated = writer.set_ai(
            default=str(default).strip() if default is not None else None,
            allow_cloud=flag("allow_cloud"),
            allow_cloud_content=flag("allow_cloud_content"),
        )
    except JarvisError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    await _apply(ctx, updated)
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="ai.settings",
            args_digest=AuditRepository.digest(
                {
                    "default": updated.ai.default,
                    "allow_cloud": updated.ai.allow_cloud,
                    "allow_cloud_content": updated.ai.allow_cloud_content,
                }
            ),
            decision="allowed",
        )
    )
    log.info(
        "ai settings changed",
        default=updated.ai.default,
        allow_cloud=updated.ai.allow_cloud,
        allow_cloud_content=updated.ai.allow_cloud_content,
    )
    return {
        "default": updated.ai.default,
        "allow_cloud": updated.ai.allow_cloud,
        "allow_cloud_content": updated.ai.allow_cloud_content,
    }


def _provider_or_404(ctx, name: str):  # type: ignore[no-untyped-def]
    """Resolve a configured provider, or 404 naming what is configured."""
    try:
        return ctx.settings.provider(name)
    except JarvisError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


def _store_key(ctx, entry: str, key: str, *, provider: str) -> None:  # type: ignore[no-untyped-def]
    """Put a key in the OS credential store, or explain why it could not.

    Whitespace is stripped because a key pasted from a browser usually carries
    a trailing newline, and a newline in an HTTP header is rejected by the
    client with an error that says nothing about the key.
    """
    cleaned = key.strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="The key is empty.")
    try:
        secrets.set_secret(entry, cleaned)
    except JarvisError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    log.info("api key stored", provider=provider, entry=entry)


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
    mic_ok, mic_detail = ctx.capture.availability()
    return {
        **status.to_dict(),
        "state": ctx.voice.state.value,
        "reason": ctx.voice.unavailable_reason(),
        # The microphone is reported separately from the models. "Voice is not
        # ready" covers two very different problems — a missing download, which
        # the interface can fix, and no audio device, which it cannot — and
        # collapsing them sends people to the wrong place.
        "microphone": {
            "available": mic_ok,
            "detail": mic_detail,
            "listening": ctx.capture.running,
            "framesSeen": ctx.capture.frames,
            "framesDropped": ctx.capture.dropped,
        },
        "micScopeGranted": Scope.MIC_LISTEN in ctx.policy.grants.granted,
        "settings": {
            "enabled": ctx.settings.voice.enabled,
            "wakeWord": ctx.settings.voice.wake_word,
            "pushToTalk": ctx.settings.voice.push_to_talk,
        },
    }


@router.post("/voice/settings")
async def set_voice_settings(request: Request) -> dict[str, object]:
    """Change `[voice]`, including whether to listen from launch.

    `enabled` is what makes the wake word behave like a wake word: on, Jarvis
    listens from the moment it starts, with no button to press each time. It is
    off by default and it is not sufficient on its own — `mic.listen` is still
    required, so turning this on cannot open a microphone the user has not
    granted.
    """
    ctx = _ctx(request)
    body = await request.json() if await request.body() else {}

    def flag(field: str) -> bool | None:
        return None if body.get(field) is None else bool(body[field])

    wake_word = body.get("wake_word")
    try:
        updated = writer.set_voice(
            enabled=flag("enabled"),
            wake_word=str(wake_word).strip() if wake_word is not None else None,
            push_to_talk=flag("push_to_talk"),
        )
    except JarvisError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    ctx.settings = updated
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="voice.settings",
            args_digest=AuditRepository.digest(
                {
                    "enabled": updated.voice.enabled,
                    "wakeWord": updated.voice.wake_word,
                    "pushToTalk": updated.voice.push_to_talk,
                }
            ),
            decision="allowed",
        )
    )

    # Said plainly rather than left for someone to discover: the setting is on,
    # the permission is not, and nothing will happen next launch.
    needs_permission = updated.voice.enabled and Scope.MIC_LISTEN not in (ctx.policy.grants.granted)
    return {
        "enabled": updated.voice.enabled,
        "wakeWord": updated.voice.wake_word,
        "pushToTalk": updated.voice.push_to_talk,
        "needsMicPermission": needs_permission,
        "listening": ctx.capture.running,
    }


@router.post("/voice/listen")
async def voice_listen(request: Request) -> dict[str, object]:
    """Open the microphone and start waiting for the wake word.

    Refused unless `mic.listen` has been granted. A continuously open microphone
    is the most invasive thing in this program, so it is not implied by voice
    being configured.

    It *can* now start at launch, which this docstring used to rule out. The
    principle it was protecting — the user asks for it once, deliberately, and
    can see it is on — is unchanged: `[voice] enabled` is that one deliberate
    ask, and `jarvis.app.start_listening_if_asked` still requires the
    permission as well. Launching is not the ask; turning the setting on is.
    """
    ctx = _ctx(request)
    if ctx.estop.engaged:
        raise HTTPException(
            status_code=409, detail="The emergency stop is engaged. Clear it first."
        )
    if Scope.MIC_LISTEN not in ctx.policy.grants.granted:
        raise HTTPException(
            status_code=403,
            detail=(
                "Listening needs the 'mic.listen' permission, which is not granted. "
                "Grant it in Permissions first."
            ),
        )
    try:
        await ctx.capture.start()
    except JarvisError as exc:
        # A missing model or an absent microphone is a 503 with the reason
        # already written for a person to read, not a stack trace.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Opening the microphone is logged like any other granted capability. The
    # hash-chained log is how "was it listening at 3pm" has an answer.
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="voice.listen",
            args_digest=AuditRepository.digest({"wakeWord": ctx.settings.voice.wake_word}),
            risk="elevated",
            decision="allowed",
        )
    )
    return {"listening": True, "state": ctx.voice.state.value}


@router.post("/voice/stt/prepare")
async def voice_prepare_stt(request: Request) -> dict[str, object]:
    """Download the speech recognition model, so listening can start.

    Its own endpoint because of a deadlock: the pipeline refused to listen while
    the model was missing, and the model was only downloaded on the first
    transcription, which required listening. There was no route from a fresh
    install to a working microphone.

    The download is ~74 MB and runs in a thread, since loading the model blocks.
    """
    import asyncio

    ctx = _ctx(request)
    status = await asyncio.to_thread(ctx.voice.stt.prepare)
    return {
        **status.to_dict(),
        "ready": ctx.voice.status().ready,
        "reason": ctx.voice.unavailable_reason(),
    }


@router.post("/voice/stop")
async def voice_stop(request: Request) -> dict[str, object]:
    """Close the microphone. Always allowed, and always works."""
    ctx = _ctx(request)
    await ctx.capture.stop()
    return {
        "listening": False,
        "state": ctx.voice.state.value,
        "framesSeen": ctx.capture.frames,
        "framesDropped": ctx.capture.dropped,
    }


@router.get("/browser/status")
async def browser_status(request: Request) -> dict[str, object]:
    """What the browser may reach, so the interface can show it rather than
    describe an intended default that may not match the config file."""
    ctx = _ctx(request)
    settings = ctx.browser.settings
    available, detail = ctx.browser.availability()
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


@router.get("/plugins")
async def list_plugins(request: Request) -> dict[str, object]:
    """Installed plugins, with what each may actually do.

    `effectiveScopes` is the intersection the host enforces — declared,
    approved, and held by Jarvis itself — so the interface shows what a plugin
    can really do rather than what it asked for.
    """
    ctx = _ctx(request)
    ctx.plugins.discover()
    return {
        "plugins": ctx.plugins.describe(ctx.policy.grants),
        "directory": str(ctx.plugins.directory),
        "isolation": (
            "A plugin runs in its own process with no inherited credentials, but as "
            "you, with your file access and your network. The scope system governs "
            "what it can do through Jarvis, not what it can do on its own."
        ),
    }


@router.post("/plugins/{name}/enable")
async def enable_plugin(request: Request, name: str) -> dict[str, object]:
    ctx = _ctx(request)
    try:
        plugin = ctx.plugins.enable(name)
    except ManifestInvalid as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    register_plugins(ctx.registry, ctx.plugins, ctx.policy.grants)
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="plugin.enable",
            args_digest=AuditRepository.digest(
                {"plugin": name, "scopes": [s.value for s in plugin.approved_scopes]}
            ),
            decision="allowed",
        )
    )
    enabled: dict[str, object] = plugin.to_dict(ctx.policy.grants)
    return enabled


@router.post("/plugins/{name}/disable")
async def disable_plugin(request: Request, name: str) -> dict[str, object]:
    ctx = _ctx(request)
    try:
        plugin = await ctx.plugins.disable(name)
    except ManifestInvalid as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="plugin.disable",
            args_digest=AuditRepository.digest({"plugin": name}),
            decision="allowed",
        )
    )
    disabled: dict[str, object] = plugin.to_dict(ctx.policy.grants)
    return disabled


@router.get("/models")
async def list_models(request: Request) -> dict[str, object]:
    """What Jarvis can download, what it costs, and what is already here."""
    del request
    return models.summary()


@router.post("/models/{key}/fetch")
async def fetch_model(request: Request, key: str) -> dict[str, object]:
    """Download one model. Refuses anything it cannot verify."""
    ctx = _ctx(request)
    spec = models.BY_KEY.get(key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"There is no model called {key!r}.")
    path = models.fetch(key)
    ctx.audit.append(
        AuditEntry(
            actor="user",
            action="model.fetch",
            args_digest=AuditRepository.digest({"model": key, "mb": spec.size_mb}),
            decision="allowed",
        )
    )
    return {"installed": True, "path": str(path), "model": spec.to_dict()}


@router.get("/models/{key}/verify")
async def verify_model(request: Request, key: str) -> dict[str, object]:
    del request
    spec = models.BY_KEY.get(key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"There is no model called {key!r}.")
    ok, detail = models.verify_installed(spec)
    return {"ok": ok, "detail": detail, "model": spec.to_dict()}


@router.delete("/models/{key}")
async def remove_model(request: Request, key: str) -> dict[str, object]:
    del request
    if key not in models.BY_KEY:
        raise HTTPException(status_code=404, detail=f"There is no model called {key!r}.")
    return {"removed": models.remove(key)}


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
    # And the microphone closes. An emergency stop that leaves it open has not
    # stopped the thing most people press it for.
    await ctx.capture.stop()
    return {"engaged": True, "cancelledPrompts": cancelled, "microphoneClosed": True}


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

    async def send_voice_event(event: object) -> None:
        """Forward a pipeline event — state changes, wake, transcript."""
        await ws.send_json(
            {"type": event.type, **event.data}  # type: ignore[attr-defined]
        )

    async def run_voice_turn(transcript: object) -> None:
        """A finished utterance becomes a turn, and the answer is spoken.

        This is the join between voice and the agent, and it deliberately goes
        through the same orchestrator as a typed message: the model gets no
        extra authority for having been spoken to, and every tool call it
        proposes is gated exactly as it would be from the keyboard.
        """
        text = str(transcript.text).strip()  # type: ignore[attr-defined]
        if not text:
            return
        session_id = voice_session["id"] or ctx.sessions.create(mode=ctx.settings.mode).id
        voice_session["id"] = session_id

        reply: list[str] = []
        try:
            async for event in ctx.orchestrator.handle(session_id, text):
                payload = event.to_json()
                payload["session_id"] = session_id
                payload["viaVoice"] = True
                await ws.send_json(payload)
                if payload["type"] == "delta":
                    reply.append(str(payload.get("text", "")))
        except JarvisError as exc:
            await ws.send_json({"type": "error", **exc.to_dict()})
            return

        # Speak the answer, then go back to waiting for the wake word. The
        # pipeline handles that transition and the barge-in while it talks.
        await ctx.voice.speak("".join(reply))

    voice_session: dict[str, str] = {"id": ""}
    ctx.voice.set_event_sink(send_voice_event)
    ctx.capture.set_transcript_handler(run_voice_turn)

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
            if kind == "voice.start":
                # Over the socket as well as over REST, because this is the
                # connection the resulting events have to come back on.
                if Scope.MIC_LISTEN not in ctx.policy.grants.granted:
                    await ws.send_json(
                        {
                            "type": "error",
                            "code": "jarvis.permission.denied",
                            "message": (
                                "Listening needs the 'mic.listen' permission, which is "
                                "not granted. Grant it in Permissions first."
                            ),
                        }
                    )
                    continue
                try:
                    await ctx.capture.start()
                except JarvisError as exc:
                    await ws.send_json({"type": "error", **exc.to_dict()})
                continue
            if kind == "voice.stop":
                await ctx.capture.stop()
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
    finally:
        # The microphone does not outlive the interface that opened it, and a
        # sink pointing at a closed socket would raise inside the pipeline's
        # state machine the next time anything changed.
        ctx.voice.set_event_sink(None)
        ctx.capture.set_transcript_handler(None)
        await ctx.capture.stop()

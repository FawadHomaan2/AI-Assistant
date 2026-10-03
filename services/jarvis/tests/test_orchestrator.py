"""Turn handling, including the interception that keeps Jarvis honest."""

from __future__ import annotations

from jarvis.agents.types import EventType, Intent


async def _run(ctx, message: str):
    session = ctx.sessions.create()
    return session, [e async for e in ctx.orchestrator.handle(session.id, message)]


async def test_chat_streams_and_persists(ctx) -> None:
    session, events = await _run(ctx, "hello there")
    kinds = [e.type for e in events]
    assert kinds[0] is EventType.TURN_START
    assert kinds[1] is EventType.ROUTE
    assert EventType.DELTA in kinds
    assert kinds[-1] is EventType.TURN_END

    history = ctx.turns.history(session.id)
    assert [t.role for t in history] == ["user", "assistant"]
    assert history[1].content  # the reply was stored, not just streamed
    assert history[1].provider == "dev_echo"


async def test_computer_task_is_intercepted_not_answered(ctx) -> None:
    """The key honesty guarantee of this phase.

    A capable model asked to open Chrome will say "Done!". Routing must stop the
    request before it reaches any model, so Jarvis cannot claim to have acted.
    """
    _, events = await _run(ctx, "Open Chrome and search for React docs")
    kinds = [e.type for e in events]
    assert EventType.NOTICE in kinds
    assert EventType.DELTA not in kinds, "a computer task must never reach the model"

    notice = next(e for e in events if e.type is EventType.NOTICE)
    assert notice.data["intent"] == Intent.COMPUTER_TASK.value
    assert notice.data["available_in_phase"] == 3
    assert "can't act on your computer yet" in notice.data["message"]


async def test_security_request_claims_no_status(ctx) -> None:
    _, events = await _run(ctx, "check my computer's security")
    notice = next(e for e in events if e.type is EventType.NOTICE)
    assert notice.data["available_in_phase"] == 9
    text = notice.data["message"].lower()
    assert "won't report a status i haven't actually measured" in text
    for claim in ("you are protected", "no threats", "all clear", "looks clean"):
        assert claim not in text


async def test_diagnostic_is_intercepted(ctx) -> None:
    _, events = await _run(ctx, "why is my laptop slow?")
    assert next(e for e in events if e.type is EventType.NOTICE).data["available_in_phase"] == 5


async def test_notice_is_persisted(ctx) -> None:
    session, _ = await _run(ctx, "Open Chrome")
    roles = [t.role for t in ctx.turns.history(session.id)]
    assert roles == ["user", "system"]


async def test_empty_message_errors(ctx) -> None:
    _, events = await _run(ctx, "   ")
    assert events[0].type is EventType.ERROR


async def test_emergency_stop_blocks_the_turn(ctx) -> None:
    ctx.estop.engage("test")
    _, events = await _run(ctx, "hello")
    assert len(events) == 1
    assert events[0].type is EventType.ERROR
    assert events[0].data["code"] == "jarvis.emergency_stop"


async def test_turn_is_audited(ctx) -> None:
    _session, _ = await _run(ctx, "hello")
    actions = [e["action"] for e in ctx.audit.recent()]
    assert "chat.message" in actions
    assert "provider.stream" in actions
    assert ctx.audit.verify() == (True, None)


async def test_history_is_replayed_to_the_model(ctx) -> None:
    session = ctx.sessions.create()
    async for _ in ctx.orchestrator.handle(session.id, "first message"):
        pass
    async for _ in ctx.orchestrator.handle(session.id, "second message"):
        pass
    assert len(ctx.turns.history(session.id)) == 4


async def test_provider_failure_is_reported_not_swallowed(ctx, monkeypatch) -> None:
    from jarvis.util.errors import ProviderUnavailable

    async def boom(*_args, **_kwargs):
        raise ProviderUnavailable("Could not reach the model server.")
        yield  # pragma: no cover

    monkeypatch.setattr(ctx.gateway, "stream", boom)
    session, events = await _run(ctx, "hello")

    error = next(e for e in events if e.type is EventType.ERROR)
    assert error.data["code"] == "jarvis.provider.unavailable"
    # The failure is recorded, so the transcript cannot imply a reply happened.
    stored = ctx.turns.history(session.id)
    assert stored[-1].error_code == "jarvis.provider.unavailable"

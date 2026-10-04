"""Turn handling, including the interception that keeps Jarvis honest."""

from __future__ import annotations

from jarvis.agents.types import EventType


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


async def test_computer_task_never_reaches_the_model(ctx) -> None:
    """The honesty guarantee that survives every phase.

    A capable model asked to open Chrome will say "Done!". A request to act is
    handled by the tool layer or refused — it is never answered by the model,
    so Jarvis cannot claim to have done something it did not do.
    """
    _, events = await _run(ctx, "Open flurbleglorp and search for React docs")
    kinds = [e.type for e in events]
    assert EventType.DELTA not in kinds, "a computer task must never reach the model"
    # It reaches the application tool, which refuses because no such program
    # exists. That is still the tool layer answering — not the model claiming
    # to have opened a browser. The name is deliberately impossible: asking for
    # Chrome made the suite launch a real browser on any machine that had one.
    assert EventType.PLAN in kinds
    assert {EventType.NOTICE, EventType.TOOL_RESULT} & set(kinds)


async def test_unmappable_file_request_says_so_rather_than_guessing(ctx) -> None:
    """Guessing here moves or deletes the wrong files."""
    _, events = await _run(ctx, "sort out my downloads however you think is best")
    kinds = [e.type for e in events]
    assert EventType.DELTA not in kinds
    notice = next(e for e in events if e.type is EventType.NOTICE)
    assert "can't yet work out the exact steps" in notice.data["message"]


async def test_security_request_is_measured_and_claims_no_more(ctx) -> None:
    """Since Phase 9 this is checked rather than deferred — and still never overstated."""
    _, events = await _run(ctx, "check my computer's security")
    result = next((e for e in events if e.type is EventType.TOOL_RESULT), None)
    assert result is not None, f"no tool ran; got {[e.type.value for e in events]}"
    data = result.data["data"]
    assert data["checksTotal"] >= 7
    # However it turns out, the reply never pronounces the machine safe.
    text = (result.data["summary"] + " " + data["headline"]).lower()
    for claim in ("you are protected", "no threats", "all clear", "looks clean", "malware"):
        assert claim not in text
    # And it always says how many checks actually ran.
    assert data["checksRun"] <= data["checksTotal"]


async def test_diagnostics_run_rather_than_being_deferred(ctx) -> None:
    """Since Phase 5 this is measured. The model is never asked to diagnose."""
    _, events = await _run(ctx, "why is my laptop slow?")
    kinds = [e.type for e in events]
    assert EventType.DELTA not in kinds, "diagnosis must not be left to the model"
    assert EventType.TOOL_RESULT in kinds

    result = next((e for e in events if e.type is EventType.TOOL_RESULT), None)
    assert result is not None, f"no tool ran; got {[e.type.value for e in events]}"
    assert result.data["data"]["findings"], "every answer must carry measured findings"
    for finding in result.data["data"]["findings"]:
        assert finding["evidence"], finding["title"]


async def test_notice_is_persisted(ctx) -> None:
    session, _ = await _run(ctx, "Open flurbleglorp")
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

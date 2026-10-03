"""The turn loop.

Takes a user message and produces a stream of events for the UI. In Phase 2 it
handles the CHAT path fully; every other intent is intercepted and answered with
an honest capability notice, because the tools that would satisfy it do not
exist yet.

That interception is load-bearing. A capable model asked to "open Chrome" will
happily reply "Done!" — so routing a computer task to the model would make
Jarvis lie the moment a real provider is configured. The agent layer, not the
prompt, is what prevents that.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from jarvis.agents.router import route
from jarvis.agents.types import INTENT_PHASE, AgentEvent, EventType, Intent
from jarvis.ai.gateway import Gateway
from jarvis.ai.types import CompletionRequest, JobClass, Message, PrivacyClass
from jarvis.db.repositories import AuditEntry, AuditRepository, SessionRepository, TurnRepository
from jarvis.governance.estop import EmergencyStop
from jarvis.util.errors import EmergencyStopped, JarvisError
from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: How much prior conversation to replay to the model.
HISTORY_LIMIT = 20

SYSTEM_PROMPT = """\
You are Jarvis, a personal AI assistant running locally on the user's Windows computer.

You are in an early build. You can converse, explain, and answer questions, but you \
CANNOT yet act on the computer: no file access, no launching applications, no system \
settings, no screenshots, no web browsing. Those tools are not built yet.

Never claim to have performed an action. If the user asks you to do something on their \
machine, say plainly that the capability is not available yet and offer to explain how \
they would do it themselves. Do not roleplay having done it.

Be concise and practical. Prefer specifics over hedging.\
"""

CAPABILITY_NOTICES: dict[Intent, str] = {
    Intent.COMPUTER_TASK: (
        "I can't act on your computer yet. Reading and writing files, launching "
        "applications and controlling windows arrive in Phase 3, behind the permission "
        "and confirmation system. I'd rather tell you that than pretend I did it."
    ),
    Intent.DIAGNOSTIC: (
        "I can't inspect your machine yet. The live CPU, memory and disk figures in the "
        "panel are real, but process inspection, startup programs and the "
        'evidence-gathering behind "why is my PC slow" arrive in Phase 5.'
    ),
    Intent.SECURITY: (
        "I can't check your security yet. Defender status, firewall state, startup "
        "items, network connections and the rest arrive in Phase 9. Until those checks "
        "are real, I won't report a status I haven't actually measured."
    ),
    Intent.MEMORY: (
        "I can't remember preferences between sessions yet — persistent memory arrives "
        "in Phase 8. This conversation is kept, but it won't shape my future behaviour."
    ),
}


class Orchestrator:
    def __init__(
        self,
        gateway: Gateway,
        sessions: SessionRepository,
        turns: TurnRepository,
        audit: AuditRepository,
        estop: EmergencyStop,
    ) -> None:
        self.gateway = gateway
        self.sessions = sessions
        self.turns = turns
        self.audit = audit
        self.estop = estop

    async def handle(self, session_id: str, message: str) -> AsyncIterator[AgentEvent]:
        """Run one turn, yielding events as they happen."""
        started = time.monotonic()
        text = message.strip()

        if self.estop.engaged:
            yield AgentEvent(
                EventType.ERROR,
                {
                    "code": EmergencyStopped.code,
                    "message": "Emergency stop is active. Clear it before Jarvis does anything.",
                },
            )
            return

        if not text:
            yield AgentEvent(
                EventType.ERROR, {"code": "jarvis.empty_message", "message": "Message was empty."}
            )
            return

        user_turn = self.turns.add(session_id, "user", text, privacy_class="content")
        yield AgentEvent(EventType.TURN_START, {"turn_id": user_turn.id, "session_id": session_id})

        decision = route(text)
        yield AgentEvent(
            EventType.ROUTE,
            {
                "intent": decision.intent.value,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "signals": decision.signals,
                "available_in_phase": INTENT_PHASE[decision.intent],
            },
        )

        self.audit.append(
            AuditEntry(
                actor="user",
                action="chat.message",
                args_digest=AuditRepository.digest({"len": len(text)}),
                decision="allowed",
                session_id=session_id,
            )
        )

        # Anything needing tools stops here, with an explanation.
        if decision.intent is not Intent.CHAT:
            notice = CAPABILITY_NOTICES[decision.intent]
            self.turns.add(session_id, "system", notice, privacy_class="metadata")
            yield AgentEvent(
                EventType.NOTICE,
                {
                    "message": notice,
                    "intent": decision.intent.value,
                    "available_in_phase": INTENT_PHASE[decision.intent],
                },
            )
            yield AgentEvent(
                EventType.TURN_END,
                {
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "handled": "capability_notice",
                },
            )
            return

        async for event in self._chat(session_id, started):
            yield event

    async def _chat(self, session_id: str, started: float) -> AsyncIterator[AgentEvent]:
        history = self.turns.history(session_id, limit=HISTORY_LIMIT)
        messages = [Message("system", SYSTEM_PROMPT)]
        for turn in history:
            if turn.role in ("user", "assistant"):
                messages.append(Message(turn.role, turn.content))  # type: ignore[arg-type]

        request = CompletionRequest(
            messages=messages, job=JobClass.CHAT, privacy=PrivacyClass.CONTENT
        )
        provider = self.gateway.provider_for(JobClass.CHAT)
        caps = provider.capabilities()

        parts: list[str] = []
        usage = None
        stop_reason = None
        try:
            async for chunk in self.gateway.stream(request):
                if self.estop.engaged:
                    raise EmergencyStopped("Emergency stop engaged mid-response.")
                if chunk.text:
                    parts.append(chunk.text)
                    yield AgentEvent(EventType.DELTA, {"text": chunk.text})
                if chunk.done:
                    usage = chunk.usage
                    stop_reason = chunk.stop_reason
        except JarvisError as exc:
            # Persist the failure so the conversation record stays truthful.
            self.turns.add(
                session_id,
                "system",
                f"[{exc.code}] {exc.message}",
                privacy_class="metadata",
                provider=caps.name,
                error_code=exc.code,
            )
            self.audit.append(
                AuditEntry(
                    actor="agent",
                    action="provider.stream",
                    outcome="error",
                    error_code=exc.code,
                    session_id=session_id,
                )
            )
            log.warning("chat failed", code=exc.code, provider=caps.name)
            yield AgentEvent(
                EventType.ERROR,
                {"code": exc.code, "message": exc.message, "provider": caps.name},
            )
            return

        reply = "".join(parts)
        elapsed = int((time.monotonic() - started) * 1000)
        self.turns.add(
            session_id,
            "assistant",
            reply,
            privacy_class="content",
            provider=caps.name,
            model=caps.model,
            tokens_in=usage.tokens_in if usage else None,
            tokens_out=usage.tokens_out if usage else None,
            latency_ms=elapsed,
        )
        self.audit.append(
            AuditEntry(
                actor="agent",
                action="provider.stream",
                args_digest=AuditRepository.digest({"provider": caps.name, "model": caps.model}),
                outcome="ok",
                session_id=session_id,
            )
        )

        yield AgentEvent(
            EventType.TURN_END,
            {
                "elapsed_ms": elapsed,
                "handled": "chat",
                "provider": caps.name,
                "model": caps.model,
                "is_cloud": caps.is_cloud,
                "tokens_in": usage.tokens_in if usage else None,
                "tokens_out": usage.tokens_out if usage else None,
                "stop_reason": stop_reason,
            },
        )

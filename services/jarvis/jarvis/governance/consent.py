"""Consent broker.

When the policy engine returns CONFIRM, the executor pauses and asks. This class
owns that round trip: it creates a pending request, hands it to the transport to
show, and waits for the answer.

The prompt contract (ARCHITECTURE §7) is enforced here, not left to each tool:
every request must say **what** (verb and exact count), **where** (the actual
paths), **why** (the originating request), **how reversible** it is, and the
**blast radius**. A tool that cannot fill those in cannot ask for consent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from jarvis.governance.policy import Decision
from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: How long a prompt waits before giving up. A question left unanswered must
#: fail closed rather than leaving an action armed indefinitely.
DEFAULT_TIMEOUT_SECONDS = 300.0

#: Longest list sent to the UI. The full count is always stated separately, so
#: truncating the preview never hides the scale of what is about to happen.
MAX_PREVIEW_TARGETS = 50

Reversibility = Literal["recycle-bin", "undoable", "permanent", "unknown"]
RememberScope = Literal["no", "session", "always"]


class ConsentDeclined(JarvisError):
    code = "jarvis.consent.declined"
    http_status = 403


class ConsentTimedOut(JarvisError):
    code = "jarvis.consent.timeout"
    http_status = 408


@dataclass
class ConsentRequest:
    id: str
    title: str
    summary: str
    risk: str
    origin: str
    targets: list[str]
    affected_count: int
    reversible: Reversibility
    blast_radius: str
    allow_remember: bool
    confirm_phrase: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "risk": self.risk,
            "origin": self.origin,
            "targets": self.targets[:MAX_PREVIEW_TARGETS],
            "affectedCount": self.affected_count,
            "reversible": self.reversible,
            "blastRadius": self.blast_radius,
            "allowRemember": self.allow_remember,
            "confirmPhrase": self.confirm_phrase,
        }


@dataclass
class ConsentAnswer:
    approved: bool
    remember: RememberScope = "no"


@dataclass
class _Pending:
    request: ConsentRequest
    future: asyncio.Future[ConsentAnswer] = field(repr=False)


class ConsentBroker:
    """Asks the user, and waits.

    `prompt` is injected by the transport: over a WebSocket it emits an event to
    the UI; in tests it is a function that answers immediately.
    """

    def __init__(
        self,
        prompt: Callable[[ConsentRequest], Awaitable[None]] | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._prompt = prompt
        self._timeout = timeout
        self._pending: dict[str, _Pending] = {}
        self._counter = 0

    def set_prompt(self, prompt: Callable[[ConsentRequest], Awaitable[None]]) -> None:
        self._prompt = prompt

    @property
    def pending(self) -> list[ConsentRequest]:
        return [p.request for p in self._pending.values()]

    def _next_id(self) -> str:
        self._counter += 1
        return f"consent_{self._counter}"

    def build(
        self,
        *,
        decision: Decision,
        title: str,
        summary: str,
        origin: str,
        targets: list[str],
        affected_count: int,
        reversible: Reversibility,
        blast_radius: str,
    ) -> ConsentRequest:
        """Assemble a request, enforcing the prompt contract."""
        if not title or not summary or not origin or not blast_radius:
            raise ValueError(
                "A consent prompt must state what, why and the blast radius. "
                "A tool that cannot fill these in must not ask."
            )
        return ConsentRequest(
            id=self._next_id(),
            title=title,
            summary=summary,
            risk=decision.risk.label,
            origin=origin,
            targets=targets,
            affected_count=affected_count,
            reversible=reversible,
            blast_radius=blast_radius,
            allow_remember=decision.allow_remember,
            confirm_phrase=decision.confirm_phrase,
        )

    async def ask(self, request: ConsentRequest) -> ConsentAnswer:
        """Show the prompt and wait. Raises rather than returning a default."""
        if self._prompt is None:
            raise ConsentDeclined(
                "This action needs your confirmation, but there is no interface "
                "connected to ask. Nothing was done."
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[ConsentAnswer] = loop.create_future()
        self._pending[request.id] = _Pending(request, future)
        log.info(
            "awaiting consent",
            consent_id=request.id,
            risk=request.risk,
            affected=request.affected_count,
        )

        try:
            await self._prompt(request)
            answer = await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError as exc:
            raise ConsentTimedOut(
                f"No answer to “{request.title}” within "
                f"{int(self._timeout / 60)} minutes, so nothing was done."
            ) from exc
        finally:
            self._pending.pop(request.id, None)

        if not answer.approved:
            raise ConsentDeclined(f"You declined “{request.title}”. Nothing was done.")
        log.info("consent granted", consent_id=request.id, remember=answer.remember)
        return answer

    def resolve(self, consent_id: str, answer: ConsentAnswer) -> bool:
        """Deliver the user's answer. Returns False if nothing was waiting."""
        pending = self._pending.get(consent_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(answer)
        return True

    def cancel_all(self, reason: str = "cancelled") -> int:
        """Decline everything outstanding — used by the emergency stop."""
        count = 0
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(ConsentAnswer(approved=False))
                count += 1
        self._pending.clear()
        if count:
            log.warning("cancelled pending consent prompts", count=count, reason=reason)
        return count

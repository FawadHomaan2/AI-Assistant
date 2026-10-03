"""Agent-layer types. These shapes are what the UI renders."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Intent(StrEnum):
    """What the user is asking for."""

    CHAT = "chat"  # conversation, questions, explanation
    COMPUTER_TASK = "computer_task"  # do something to the machine (needs tools)
    DIAGNOSTIC = "diagnostic"  # inspect machine state (needs tools)
    SECURITY = "security"  # security posture (needs tools)
    MEMORY = "memory"  # remember / forget a preference


#: Intent -> the phase that makes it actually work. CHAT works now.
INTENT_PHASE: dict[Intent, int] = {
    Intent.CHAT: 2,
    Intent.COMPUTER_TASK: 3,
    Intent.DIAGNOSTIC: 5,
    Intent.SECURITY: 9,
    Intent.MEMORY: 8,
}


@dataclass
class Route:
    intent: Intent
    confidence: float
    reason: str
    #: Phrases that drove the decision, so the UI can explain itself.
    signals: list[str] = field(default_factory=list)


class EventType(StrEnum):
    TURN_START = "turn.start"
    ROUTE = "route"
    DELTA = "delta"
    NOTICE = "notice"
    ERROR = "error"
    TURN_END = "turn.end"
    # Phase 3: tool execution.
    PLAN = "plan"
    TOOL_PLANNED = "tool.planned"
    TOOL_RESULT = "tool.result"
    CONSENT_REQUEST = "consent.request"
    # Phase 8: memory. Emitted whenever something is recalled, learned or
    # forgotten, so a belief that shapes an answer is never invisible.
    MEMORY_RECALLED = "memory.recalled"
    MEMORY_LEARNED = "memory.learned"
    MEMORY_FORGOTTEN = "memory.forgotten"


@dataclass
class AgentEvent:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type.value, **self.data}

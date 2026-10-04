"""Egress control.

Runs before any network call to a model provider. Two jobs:

1. Refuse outright to send SENSITIVE data off the machine. Credentials,
   security findings, and the audit log are in that class. This raises rather
   than redacting, because a redaction bug fails open and a type error fails
   closed.
2. Redact and size-cap everything that is allowed to leave.
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.ai.types import CompletionRequest, Message, PrivacyClass
from jarvis.util.errors import PrivacyViolation
from jarvis.util.logging import get_logger
from jarvis.util.redaction import redact_text

log = get_logger(__name__)

# A single payload larger than this is almost certainly an accident.
MAX_EGRESS_CHARS = 400_000


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    reason: str
    redacted_chars: int = 0


def check(
    request: CompletionRequest,
    *,
    provider_name: str,
    is_cloud: bool,
    allow_cloud: bool,
    allow_cloud_content: bool,
) -> EgressDecision:
    """Decide whether this request may be sent to this provider."""
    if not is_cloud:
        return EgressDecision(True, "local provider; nothing leaves the machine")

    if request.privacy is PrivacyClass.SENSITIVE:
        raise PrivacyViolation(
            "This request carries sensitive data (credentials, security findings "
            "or audit records), which is never sent to a cloud provider. "
            "Use a local model for this.",
            provider=provider_name,
            job=request.job.value,
        )

    if not allow_cloud:
        raise PrivacyViolation(
            f"Cloud AI is turned off, so nothing can be sent to {provider_name!r}. "
            "Enable it in Settings, or select a local provider.",
            provider=provider_name,
        )

    if request.privacy is PrivacyClass.CONTENT and not allow_cloud_content:
        raise PrivacyViolation(
            "This request contains file or screen content. Sending document "
            "content to a cloud provider needs its own permission — enable "
            "'Send file content to cloud AI' in Settings, or use a local model.",
            provider=provider_name,
        )

    return EgressDecision(True, "permitted by privacy settings")


def sanitise(request: CompletionRequest) -> tuple[CompletionRequest, int]:
    """Redact secrets and cap size. Returns the request and chars removed."""
    before = sum(len(m.content) for m in request.messages)
    messages: list[Message] = []
    for m in request.messages:
        text = redact_text(m.content)
        if len(text) > MAX_EGRESS_CHARS:
            text = text[:MAX_EGRESS_CHARS] + "\n[truncated: payload exceeded egress size cap]"
        messages.append(Message(role=m.role, content=text))
    after = sum(len(m.content) for m in messages)

    sanitised = CompletionRequest(
        messages=messages,
        job=request.job,
        privacy=request.privacy,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        stop=list(request.stop),
    )
    return sanitised, max(0, before - after)

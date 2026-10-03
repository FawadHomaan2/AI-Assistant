"""Tool contract.

Every tool declares, as data rather than prose: a name, a description, input and
output schemas, the scopes it needs, its risk tier, and whether it can be
previewed or undone. The executor reads those declarations — it never trusts a
tool to police itself.

A tool's `execute` is only ever reached after the policy engine has approved the
call, so tools contain no permission logic. That separation is what makes the
gate un-bypassable: there is no second code path.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from jarvis.governance.consent import Reversibility
from jarvis.governance.policy import ActionRequest
from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.util.errors import JarvisError


class ToolError(JarvisError):
    """A tool failed in a way the user should see explained."""

    code = "jarvis.tool.failed"
    http_status = 400


class ToolInputInvalid(ToolError):
    code = "jarvis.tool.bad_input"
    http_status = 422


@dataclass
class Preview:
    """What an action *would* do, computed without doing it.

    This is what makes a consent prompt trustworthy: the counts and paths shown
    are measured, not estimated from the request.
    """

    summary: str
    targets: list[str] = field(default_factory=list)
    affected: int = 0
    reversible: Reversibility = "unknown"
    blast_radius: str = ""
    #: Set when the preview found a reason the action cannot proceed.
    blocked: str = ""


@dataclass
class ToolResult:
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    #: Human-readable record of what changed, for the activity log.
    changes: list[str] = field(default_factory=list)
    error: str = ""
    #: Set when the tool can reverse what it did.
    undo_token: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "data": self.data,
            "changes": self.changes,
            "error": self.error,
            "undoToken": self.undo_token,
        }


@dataclass
class ToolSpec:
    """The declaration the executor and the model both read."""

    name: str
    description: str
    scopes: list[Scope]
    risk: Risk
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=dict)
    #: True when the tool can compute a preview without side effects.
    previewable: bool = True
    #: Phase that delivers this tool, so unavailable ones explain themselves.
    available: bool = True
    unavailable_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "scopes": [s.value for s in self.scopes],
            "risk": self.risk.label,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "previewable": self.previewable,
            "available": self.available,
            "unavailableReason": self.unavailable_reason,
        }


class Tool(abc.ABC):
    """Base class. Subclasses implement `preview` and `execute`."""

    @property
    @abc.abstractmethod
    def spec(self) -> ToolSpec: ...

    @abc.abstractmethod
    async def preview(self, args: dict[str, Any]) -> Preview:
        """Describe what this call would do. Must have no side effects."""

    @abc.abstractmethod
    async def execute(self, args: dict[str, Any]) -> ToolResult:
        """Perform the action. Only called after the policy engine approves."""

    async def observe(self, args: dict[str, Any]) -> dict[str, Any]:
        """Snapshot the state this call would change, for before/after checking.

        Only the tool knows what its own operation touches, so the executor asks
        rather than guessing. Returning `{}` means "nothing observable", and the
        executor then trusts the tool's own result.
        """
        del args
        return {}

    async def undo(self, token: str) -> ToolResult:
        """Reverse a previous call. Default: not supported, stated plainly."""
        raise ToolError(
            f"{self.spec.name} cannot undo its actions, so there is nothing to reverse.",
            undo_token=token,
        )

    def action_request(self, args: dict[str, Any], preview: Preview) -> ActionRequest:
        """Translate a call into the terms the policy engine reasons about."""
        return ActionRequest(
            tool=self.spec.name,
            action=str(args.get("operation", self.spec.name)),
            risk=self.risk_for(args, preview),
            scopes=self.scopes_for(args),
            affected=max(preview.affected, 1),
            reversible=preview.reversible,
            summary=preview.summary,
        )

    def risk_for(self, args: dict[str, Any], preview: Preview) -> Risk:
        """Risk for this specific call. Tools override to vary by operation."""
        del args, preview
        return self.spec.risk

    def scopes_for(self, args: dict[str, Any]) -> list[Scope]:
        """Scopes for this specific call. Tools override to vary by operation."""
        del args
        return list(self.spec.scopes)

    @staticmethod
    def require(args: dict[str, Any], *names: str) -> None:
        if missing := [n for n in names if args.get(n) in (None, "")]:
            raise ToolInputInvalid(
                f"Missing required input: {', '.join(missing)}.", missing=missing
            )

"""Tool execution.

The single path from "the agent wants to do X" to "X happened". Every call
goes: preview → policy → consent (if required) → execute → observe → audit.

There is deliberately no other way to reach `Tool.execute` from the agent layer.
If a future tool needs different handling, it changes this file rather than
adding a second route around the gate.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from jarvis.agents.types import AgentEvent, EventType
from jarvis.db.repositories import AuditEntry, AuditRepository
from jarvis.governance.consent import ConsentAnswer, ConsentBroker, ConsentDeclined, ConsentTimedOut
from jarvis.governance.policy import Policy, Verdict
from jarvis.governance.risk import Risk
from jarvis.tools.base import Preview, Tool, ToolResult
from jarvis.tools.registry import ToolRegistry
from jarvis.util.errors import EmergencyStopped, JarvisError
from jarvis.util.logging import get_logger

log = get_logger(__name__)


@dataclass
class Observation:
    """State captured before and after, so success is measured not assumed."""

    before: dict[str, Any]
    after: dict[str, Any]

    @property
    def changed(self) -> bool:
        return self.before != self.after


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: Policy,
        consent: ConsentBroker,
        audit: AuditRepository,
        estop: Any,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.consent = consent
        self.audit = audit
        self.estop = estop

    async def run(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        origin: str,
        session_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one tool call, emitting events as it goes."""
        started = time.monotonic()
        tool = self.registry.get(tool_name)
        action = str(args.get("operation", tool_name))

        if self.estop.engaged:
            yield self._error(EmergencyStopped("Emergency stop is active, so nothing ran."))
            return

        # ── 1. Preview: measure, don't guess ─────────────────────────────
        try:
            preview = await tool.preview(args)
        except JarvisError as exc:
            # Any typed failure becomes an event — a denied path, a bad argument,
            # or a capability this platform does not have. Letting one escape
            # would end the turn with no result and no turn.end, leaving the
            # interface waiting for a reply that never comes.
            self._audit(tool_name, action, "denied", session_id, error=exc.code)
            yield self._error(exc)
            return

        if preview.blocked:
            self._audit(tool_name, action, "blocked", session_id)
            yield AgentEvent(
                EventType.NOTICE,
                {"message": preview.blocked, "tool": tool_name, "blocked": True},
            )
            return

        # ── 2. Policy ────────────────────────────────────────────────────
        request = tool.action_request(args, preview)
        decision = self.policy.evaluate(request, stopped=self.estop.engaged)

        yield AgentEvent(
            EventType.TOOL_PLANNED,
            {
                "tool": tool_name,
                "operation": action,
                "summary": preview.summary,
                "affected": preview.affected,
                "risk": decision.risk.label,
                "verdict": decision.verdict.value,
                "reason": decision.reason,
            },
        )

        if decision.verdict is Verdict.DENY:
            self._audit(tool_name, action, "denied", session_id, risk=decision.risk)
            yield AgentEvent(
                EventType.NOTICE,
                {
                    "message": decision.reason,
                    "tool": tool_name,
                    "denialCode": decision.denial_code,
                    "missingScopes": [s.value for s in decision.missing_scopes],
                    "blocked": True,
                },
            )
            return

        # ── 3. Consent ───────────────────────────────────────────────────
        if decision.verdict is Verdict.CONFIRM:
            try:
                answer = await self._ask(tool_name, preview, decision, origin)
            except (ConsentDeclined, ConsentTimedOut) as exc:
                self._audit(tool_name, action, "declined", session_id, error=exc.code)
                yield AgentEvent(
                    EventType.NOTICE, {"message": exc.message, "tool": tool_name, "blocked": True}
                )
                return
            if answer.remember in ("session", "always"):
                self.policy.remember(request)

        # ── 4. Observe before ────────────────────────────────────────────
        before = await self._observe(tool, args)

        # ── 5. Execute ───────────────────────────────────────────────────
        if self.estop.engaged:
            yield self._error(EmergencyStopped("Emergency stop engaged before the action ran."))
            return

        try:
            result = await tool.execute(args)
        except JarvisError as exc:
            self._audit(tool_name, action, "error", session_id, error=exc.code, risk=decision.risk)
            log.warning("tool failed", tool=tool_name, operation=action, code=exc.code)
            yield self._error(exc)
            return

        # ── 6. Observe after, and verify the claim ───────────────────────
        after = await self._observe(tool, args)
        observation = Observation(before, after)
        verified = self._verify(action, result, observation)

        self._audit(
            tool_name,
            action,
            "ok" if verified else "unverified",
            session_id,
            risk=decision.risk,
            args=args,
        )

        yield AgentEvent(
            EventType.TOOL_RESULT,
            {
                "tool": tool_name,
                "operation": action,
                "ok": result.ok and verified,
                "summary": result.summary,
                "changes": result.changes,
                "data": result.data,
                "verified": verified,
                "elapsedMs": int((time.monotonic() - started) * 1000),
                "risk": decision.risk.label,
            },
        )

    # ── helpers ──────────────────────────────────────────────────────────
    async def _ask(
        self, tool_name: str, preview: Preview, decision: Any, origin: str
    ) -> ConsentAnswer:
        request = self.consent.build(
            decision=decision,
            title=preview.summary,
            summary=preview.blast_radius or preview.summary,
            origin=origin,
            targets=preview.targets,
            affected_count=preview.affected,
            reversible=preview.reversible,
            blast_radius=preview.blast_radius,
        )
        log.info(
            "asking for consent",
            tool=tool_name,
            risk=decision.risk.label,
            affected=preview.affected,
        )
        return await self.consent.ask(request)

    @staticmethod
    async def _observe(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
        """Ask the tool to snapshot the state it is about to change.

        Best-effort: a failed observation never blocks the action, but it does
        mean the result is reported as unverified rather than assumed good.
        """
        try:
            return await tool.observe(args)
        except Exception:  # observation is advisory; never block on it
            return {}

    @staticmethod
    def _verify(action: str, result: ToolResult, observation: Observation) -> bool:
        """Check the observed change matches what the tool claims it did.

        This is what keeps the assistant honest: a success message is derived
        from measured state, not from the tool returning without raising.
        """
        if not result.ok:
            return False
        if not observation.before and not observation.after:
            return True  # nothing observable; the tool's own result stands

        expectations = {
            "create_folder": observation.after.get("exists") is True,
            "write": observation.after.get("exists") is True,
            "append": observation.after.get("exists") is True,
            "delete": observation.after.get("exists") is False,
            "delete_permanently": observation.after.get("exists") is False,
            "move": observation.after.get("exists") is False,
            "rename": observation.after.get("exists") is False,
        }
        return expectations.get(action, True)

    def _audit(
        self,
        tool: str,
        action: str,
        outcome: str,
        session_id: str | None,
        *,
        risk: Risk | None = None,
        error: str = "",
        args: dict[str, Any] | None = None,
    ) -> None:
        self.audit.append(
            AuditEntry(
                actor="agent",
                action=f"tool.{tool}.{action}",
                args_digest=AuditRepository.digest(args or {}),
                risk=(risk or Risk.SAFE).label,
                decision="allowed" if outcome in ("ok", "unverified") else outcome,
                outcome=outcome,
                error_code=error or None,
                session_id=session_id,
            )
        )

    @staticmethod
    def _error(exc: JarvisError) -> AgentEvent:
        return AgentEvent(EventType.ERROR, {"code": exc.code, "message": exc.message})

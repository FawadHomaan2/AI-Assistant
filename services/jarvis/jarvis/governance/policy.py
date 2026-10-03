"""The policy engine.

Every tool call passes through `Policy.evaluate` before it runs. There is no
code path from the executor to a tool that skips it — that is the single
invariant the whole safety model rests on.

A decision combines three independent axes (ARCHITECTURE §7). All three must
permit an action:

  1. **Risk tier** — a property of what the action does.
  2. **Scopes** — what the user has granted.
  3. **Mode** — the global posture (paused / guarded / assisted / developer).

Plus two hard constraints that no setting can relax: the path jail, and the
emergency stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from jarvis.governance.limits import RateLimiter
from jarvis.governance.risk import MODE_AUTO_CEILING, Risk
from jarvis.governance.scopes import DESCRIPTIONS, NEVER_BLANKET, PATH_SCOPES, Scope, ScopeGrants
from jarvis.util.logging import get_logger

log = get_logger(__name__)


class Verdict(StrEnum):
    ALLOW = "allow"  # run it now
    CONFIRM = "confirm"  # ask the user first
    DENY = "deny"  # refuse; no prompt can rescue it
    #: Read-only mode: report what the action WOULD do, and do nothing.
    #: Distinct from DENY because the user gets the answer they wanted —
    #: the preview — rather than a refusal they have to work around.
    DRY_RUN = "dry_run"


@dataclass
class Decision:
    verdict: Verdict
    risk: Risk
    reason: str
    #: Set when the verdict is DENY, so the UI can explain and offer a remedy.
    denial_code: str = ""
    missing_scopes: list[Scope] = field(default_factory=list)
    #: Only meaningful for CONFIRM.
    allow_remember: bool = False
    confirm_phrase: str = ""
    #: Set when the verdict is DENY for a rate limit.
    retry_after_seconds: float = 0.0

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "risk": self.risk.label,
            "reason": self.reason,
            "denial_code": self.denial_code,
            "missing_scopes": [s.value for s in self.missing_scopes],
            "allow_remember": self.allow_remember,
            "confirm_phrase": self.confirm_phrase,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass
class ActionRequest:
    """What a tool wants to do, in the terms the policy engine reasons about."""

    tool: str
    action: str
    risk: Risk
    scopes: list[Scope]
    #: Number of objects affected. Drives escalation on bulk operations.
    affected: int = 1
    #: Whether the effect can be undone, which the consent prompt must state.
    reversible: str = "unknown"
    summary: str = ""
    #: The paths or objects this acts on. Path scopes are checked against each
    #: one; without them a folder-limited grant could not be enforced.
    targets: list[str] = field(default_factory=list)


#: A bulk operation is riskier than the same operation on one item: "delete 4000
#: files" deserves more friction than "delete this file", even though the
#: underlying call is identical.
BULK_THRESHOLD = 25
CRITICAL_BULK_THRESHOLD = 200


class Policy:
    def __init__(
        self,
        grants: ScopeGrants | None = None,
        mode: str = "guarded",
        *,
        read_only: bool = False,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.grants = grants if grants is not None else ScopeGrants()
        self.mode = mode
        #: Hard override: every mutating action becomes a dry run.
        self.read_only = read_only
        #: The backstop behind the other two axes: scopes and confirmation both
        #: assume a human in the loop, and a runaway agent loop has none.
        self.limiter = limiter or RateLimiter()
        #: Per-session approvals, keyed by the tool/action pair.
        self._remembered: set[str] = set()

    # ── remembering ──────────────────────────────────────────────────────
    @staticmethod
    def _key(request: ActionRequest) -> str:
        return f"{request.tool}:{request.action}"

    def remember(self, request: ActionRequest) -> None:
        """Record a scoped approval. Refused above medium risk, by design."""
        if not request.risk.allows_remember:
            log.warning(
                "refused to remember a high-risk approval",
                tool=request.tool,
                risk=request.risk.label,
            )
            return
        self._remembered.add(self._key(request))

    def forget_all(self) -> None:
        self._remembered.clear()

    # ── the gate ─────────────────────────────────────────────────────────
    def effective_risk(self, request: ActionRequest) -> Risk:
        """Escalate the declared tier for bulk operations."""
        risk = request.risk
        if request.affected >= CRITICAL_BULK_THRESHOLD and risk >= Risk.MEDIUM:
            return Risk.CRITICAL
        if request.affected >= BULK_THRESHOLD and risk >= Risk.LOW:
            return Risk(min(Risk.CRITICAL, risk + 1))
        return risk

    def evaluate(self, request: ActionRequest, *, stopped: bool = False) -> Decision:
        risk = self.effective_risk(request)

        # 1. Emergency stop beats everything.
        if stopped:
            return Decision(
                Verdict.DENY,
                risk,
                "Emergency stop is active, so nothing runs.",
                denial_code="emergency_stop",
            )

        # 2. Read-only turns every mutating action into a dry run. Not a
        #    denial: the user still gets the answer — what this would have done
        #    — which is the whole point of the mode.
        if self.read_only and risk > Risk.SAFE:
            return Decision(
                Verdict.DRY_RUN,
                risk,
                "Read-only mode is on. Jarvis will say what this would do and not do it.",
                denial_code="read_only",
            )

        # 3. Paused executes nothing beyond inspection.
        if self.mode == "paused" and risk > Risk.SAFE:
            return Decision(
                Verdict.DENY,
                risk,
                "Jarvis is paused. Switch to Guarded mode to let actions run.",
                denial_code="paused",
            )

        # 4. Scopes. A missing grant is a denial, not a prompt: the user grants
        #    capabilities in Privacy settings deliberately, not under time
        #    pressure in the middle of a task.
        if missing := self.grants.missing(request.scopes, request.targets):
            names = ", ".join(DESCRIPTIONS.get(s, s.value) for s in missing)
            return Decision(
                Verdict.DENY,
                risk,
                f"This needs permission Jarvis does not have: {names}. "
                + (
                    f"The grant you have does not cover {request.targets[0]}. "
                    if any(s in PATH_SCOPES for s in missing) and request.targets
                    else ""
                )
                + "Grant it in Privacy settings if you want this to work.",
                denial_code="missing_scope",
                missing_scopes=missing,
            )

        # 5. Rate limits. A refusal, never a queue: silently delaying what the
        #    user asked for would hide the loop that caused it.
        if not (budget := self.limiter.check(risk)).allowed:
            return Decision(
                Verdict.DENY,
                risk,
                budget.reason,
                denial_code="rate_limited",
                retry_after_seconds=budget.retry_after_seconds,
            )

        # 6. Risk against the mode's ceiling.
        ceiling = MODE_AUTO_CEILING.get(self.mode, Risk.LOW)

        if risk >= Risk.CRITICAL:
            # Never auto-approved, never remembered, whatever the mode says.
            return Decision(
                Verdict.CONFIRM,
                risk,
                self._confirm_reason(request, risk),
                allow_remember=False,
                confirm_phrase=self._phrase_for(request),
            )

        if blanket := [s for s in request.scopes if s in NEVER_BLANKET]:
            # Some capabilities cannot be authorised in advance: what is being
            # authorised depends entirely on what is sent or run, which a
            # standing grant cannot describe. These confirm every time.
            names = ", ".join(DESCRIPTIONS.get(s, s.value) for s in blanket)
            return Decision(
                Verdict.CONFIRM,
                risk,
                f"{names} is confirmed every time, whatever the mode — what it does "
                f"depends on the specific request.",
                allow_remember=False,
                confirm_phrase=self._phrase_for(request) if risk.needs_typed_phrase else "",
            )

        if risk <= ceiling:
            return Decision(
                Verdict.ALLOW, risk, f"{risk.label} action, allowed in {self.mode} mode"
            )

        if self._key(request) in self._remembered and risk.allows_remember:
            return Decision(Verdict.ALLOW, risk, "you approved this action earlier in this session")

        return Decision(
            Verdict.CONFIRM,
            risk,
            self._confirm_reason(request, risk),
            allow_remember=risk.allows_remember,
        )

    def _confirm_reason(self, request: ActionRequest, risk: Risk) -> str:
        if risk > request.risk:
            return (
                f"{request.affected} items makes this a {risk.label}-risk action "
                f"rather than {request.risk.label}"
            )
        return f"{risk.label}-risk action needs confirmation in {self.mode} mode"

    @staticmethod
    def _phrase_for(request: ActionRequest) -> str:
        """The word the user types to enable Confirm on a critical action."""
        if "delete" in request.action:
            return "DELETE"
        if "overwrite" in request.action or "replace" in request.action:
            return "OVERWRITE"
        return "CONFIRM"

    def describe(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "read_only": self.read_only,
            "limits": self.limiter.describe(),
            "auto_ceiling": MODE_AUTO_CEILING.get(self.mode, Risk.LOW).label,
            "remembered": sorted(self._remembered),
            "scopes": self.grants.describe(),
        }

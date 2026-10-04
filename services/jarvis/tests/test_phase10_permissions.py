"""Red-teaming the gate.

The Phase 10 gate is a claim with no middle ground: **no tool is reachable
without passing the permission engine**. These tests try to get past it, by
every route that looked plausible while writing it:

  - calling a tool directly from the agent layer, bypassing the executor;
  - a path-scoped grant that leaks to a sibling or a parent folder;
  - an expired grant that still works because nothing swept it;
  - a "remember this" approval that survives revoking the capability;
  - escalating past the mode ceiling through bulk size;
  - a tier-5 action approved by a setting, a mode, or a remembered answer;
  - a runaway loop grinding through a thousand approvals.

A test here failing is not a style problem. It means the permission model has a
hole.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

from jarvis.agents.executor import Executor
from jarvis.agents.types import EventType
from jarvis.db.engine import Database
from jarvis.db.repositories import AuditRepository
from jarvis.governance.consent import ConsentAnswer, ConsentBroker
from jarvis.governance.estop import EmergencyStop
from jarvis.governance.limits import PER_MINUTE, PROMPTS_PER_MINUTE, RateLimiter
from jarvis.governance.persistence import GrantStore
from jarvis.governance.policy import ActionRequest, Policy, Verdict
from jarvis.governance.risk import Risk
from jarvis.governance.scopes import (
    DEFAULT_GRANTS,
    NEVER_BLANKET,
    PATH_SCOPES,
    Grant,
    Scope,
    ScopeGrants,
    covers,
    normalise,
)
from jarvis.tools.base import Preview, Tool, ToolResult, ToolSpec
from jarvis.tools.registry import ToolRegistry


def request(
    risk: Risk = Risk.MEDIUM,
    scopes: list[Scope] | None = None,
    *,
    affected: int = 1,
    targets: list[str] | None = None,
    action: str = "write",
) -> ActionRequest:
    return ActionRequest(
        tool="filesystem",
        action=action,
        risk=risk,
        scopes=scopes if scopes is not None else [Scope.FS_WRITE],
        affected=affected,
        targets=targets or [],
    )


def permissive() -> Policy:
    return Policy(ScopeGrants.of(set(Scope)), mode="assisted")


# ── path-scoped grants ───────────────────────────────────────────────────
class TestPathScopes:
    def test_a_folder_grant_covers_its_children(self) -> None:
        grants = ScopeGrants(grants=[Grant(Scope.FS_WRITE, "/home/me/Desktop")])
        assert grants.has(Scope.FS_WRITE, "/home/me/Desktop/notes/todo.txt")

    def test_a_folder_grant_does_not_cover_a_sibling(self) -> None:
        grants = ScopeGrants(grants=[Grant(Scope.FS_WRITE, "/home/me/Desktop")])
        assert not grants.has(Scope.FS_WRITE, "/home/me/Documents/cv.docx")

    def test_a_folder_grant_does_not_cover_its_parent(self) -> None:
        """Granting a subfolder must not hand over everything above it."""
        grants = ScopeGrants(grants=[Grant(Scope.FS_WRITE, "/home/me/Desktop/work")])
        assert not grants.has(Scope.FS_WRITE, "/home/me/Desktop/private.txt")

    @pytest.mark.parametrize(
        ("granted", "requested"),
        [
            ("/home/me/doc", "/home/me/documents/x"),  # prefix, not a child
            ("/home/me/doc", "/home/me/doc2"),
            ("/home/me/a", "/home/me/ab/c"),
        ],
    )
    def test_a_name_prefix_is_not_containment(self, granted: str, requested: str) -> None:
        """A naive startswith() would hand over the folder next door."""
        assert covers(granted, requested) is False

    def test_traversal_in_the_request_does_not_escape_the_grant(self) -> None:
        grants = ScopeGrants(grants=[Grant(Scope.FS_WRITE, "/home/me/Desktop")])
        assert not grants.has(Scope.FS_WRITE, "/home/me/Desktop/../Documents/cv.docx")

    def test_normalisation_is_consistent(self) -> None:
        assert normalise("/home/me/Desktop/") == normalise("/home/me/Desktop")
        assert normalise("/home/me/./Desktop") == normalise("/home/me/Desktop")

    def test_every_target_must_be_covered_not_just_one(self) -> None:
        """A partial check is how a permission system becomes worthless."""
        grants = ScopeGrants(grants=[Grant(Scope.FS_WRITE, "/home/me/Desktop")])
        missing = grants.missing(
            [Scope.FS_WRITE], ["/home/me/Desktop/a.txt", "/home/me/Documents/b.txt"]
        )
        assert missing == [Scope.FS_WRITE]

    def test_the_policy_enforces_targets_not_just_scope_names(self) -> None:
        policy = Policy(ScopeGrants(grants=[Grant(Scope.FS_WRITE, "/home/me/Desktop")]))
        allowed = policy.evaluate(request(Risk.LOW, targets=["/home/me/Desktop/a.txt"]))
        denied = policy.evaluate(request(Risk.LOW, targets=["/etc/passwd"]))
        assert allowed.verdict is Verdict.ALLOW
        assert denied.verdict is Verdict.DENY
        assert denied.denial_code == "missing_scope"

    def test_an_untargeted_grant_still_leaves_the_path_jail(self) -> None:
        """A broad grant is not a way around the folder jail; they are separate."""
        policy = Policy(ScopeGrants.of({Scope.FS_WRITE}))
        assert policy.evaluate(request(Risk.LOW, targets=["/etc/passwd"])).verdict is (
            Verdict.ALLOW
        )
        # ...and the jail refuses it independently. Asserted in test_pathjail.py;
        # this test exists to record that the grant alone is not sufficient.
        from jarvis.governance.pathjail import PathDenied, PathJail

        with pytest.raises(PathDenied):
            PathJail([Path("/home/me/Desktop")]).check("/etc/passwd")


# ── expiry ───────────────────────────────────────────────────────────────
class TestExpiry:
    def test_an_expired_grant_does_not_permit(self) -> None:
        grants = ScopeGrants.of(set())
        grants.grant(Scope.FS_DELETE, ttl_minutes=-1)
        assert not grants.has(Scope.FS_DELETE)

    def test_expiry_is_checked_on_use_not_only_when_swept(self) -> None:
        """A grant that is never swept is a grant that still works."""
        grants = ScopeGrants(
            grants=[Grant(Scope.FS_DELETE, expires_at="2000-01-01T00:00:00+00:00")]
        )
        assert grants.granted == set()
        assert (
            Policy(grants).evaluate(request(Risk.LOW, [Scope.FS_DELETE])).denial_code
            == "missing_scope"
        )

    def test_an_unreadable_expiry_fails_closed(self) -> None:
        """ "I cannot tell when this stops" has only one safe reading."""
        assert Grant(Scope.FS_DELETE, expires_at="not-a-date").expired is True

    def test_a_live_ttl_grant_works(self) -> None:
        grants = ScopeGrants.of(set())
        grants.grant(Scope.FS_DELETE, ttl_minutes=60)
        assert grants.has(Scope.FS_DELETE)


# ── the gate itself ──────────────────────────────────────────────────────
class TestTheGate:
    def test_nothing_runs_under_the_emergency_stop(self) -> None:
        for risk in Risk:
            decision = permissive().evaluate(request(risk), stopped=True)
            assert decision.verdict is Verdict.DENY
            assert decision.denial_code == "emergency_stop"

    def test_paused_executes_nothing_mutating(self) -> None:
        policy = Policy(ScopeGrants.of(set(Scope)), mode="paused")
        for risk in (Risk.LOW, Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL):
            assert policy.evaluate(request(risk)).verdict is not Verdict.ALLOW

    def test_critical_always_confirms_whatever_the_mode(self) -> None:
        for mode in ("guarded", "assisted", "developer"):
            policy = Policy(ScopeGrants.of(set(Scope)), mode=mode)
            decision = policy.evaluate(request(Risk.CRITICAL))
            assert decision.verdict is Verdict.CONFIRM
            assert decision.allow_remember is False
            assert decision.confirm_phrase, "tier 5 needs a typed phrase"

    def test_critical_cannot_be_remembered_even_if_asked(self) -> None:
        policy = permissive()
        policy.remember(request(Risk.CRITICAL))
        assert policy.evaluate(request(Risk.CRITICAL)).verdict is Verdict.CONFIRM

    def test_high_risk_cannot_be_remembered(self) -> None:
        policy = permissive()
        policy.remember(request(Risk.HIGH))
        assert policy.evaluate(request(Risk.HIGH)).verdict is Verdict.CONFIRM

    def test_bulk_size_escalates_past_the_ceiling(self) -> None:
        policy = Policy(ScopeGrants.of(set(Scope)), mode="assisted")
        assert policy.evaluate(request(Risk.MEDIUM)).verdict is Verdict.ALLOW
        assert policy.evaluate(request(Risk.MEDIUM, affected=500)).verdict is Verdict.CONFIRM

    def test_escalation_cannot_be_dodged_by_a_remembered_answer(self) -> None:
        policy = Policy(ScopeGrants.of(set(Scope)), mode="assisted")
        policy.remember(request(Risk.MEDIUM))
        escalated = policy.evaluate(request(Risk.MEDIUM, affected=500))
        assert escalated.verdict is Verdict.CONFIRM

    def test_a_missing_scope_is_a_denial_not_a_prompt(self) -> None:
        """Capabilities are granted deliberately, not under time pressure."""
        decision = Policy(ScopeGrants.of(set())).evaluate(request(Risk.SAFE))
        assert decision.verdict is Verdict.DENY
        assert decision.missing_scopes == [Scope.FS_WRITE]

    @pytest.mark.parametrize("scope", sorted(NEVER_BLANKET))
    def test_some_capabilities_confirm_every_time(self, scope: Scope) -> None:
        """What they authorise depends on the specific request, so a standing
        grant cannot describe it."""
        policy = Policy(ScopeGrants.of(set(Scope)), mode="developer")
        decision = policy.evaluate(request(Risk.SAFE, [scope]))
        assert decision.verdict is Verdict.CONFIRM
        assert decision.allow_remember is False

    def test_the_install_default_grants_nothing_dangerous(self) -> None:
        dangerous = {
            Scope.FS_DELETE,
            Scope.PROCESS_KILL,
            Scope.SYSTEM_SETTINGS,
            Scope.SYSTEM_ADMIN,
            Scope.SHELL_RUN,
            Scope.SHELL_POWERSHELL,
            Scope.BROWSER_USE,
            Scope.SCREEN_CAPTURE,
            Scope.MIC_LISTEN,
            Scope.CLOUD_LLM,
            Scope.CLOUD_SEND,
            Scope.SECURITY_MODIFY,
        }
        assert dangerous & DEFAULT_GRANTS == set()


# ── read-only ────────────────────────────────────────────────────────────
class TestReadOnly:
    def test_read_only_turns_mutation_into_a_dry_run(self) -> None:
        policy = Policy(ScopeGrants.of(set(Scope)), mode="developer", read_only=True)
        assert policy.evaluate(request(Risk.MEDIUM)).verdict is Verdict.DRY_RUN

    def test_read_only_still_allows_reading(self) -> None:
        policy = Policy(ScopeGrants.of(set(Scope)), read_only=True)
        assert policy.evaluate(request(Risk.SAFE)).verdict is Verdict.ALLOW

    def test_read_only_outranks_every_mode(self) -> None:
        for mode in ("guarded", "assisted", "developer"):
            policy = Policy(ScopeGrants.of(set(Scope)), mode=mode, read_only=True)
            assert policy.evaluate(request(Risk.LOW)).verdict is Verdict.DRY_RUN


# ── rate limits ──────────────────────────────────────────────────────────
class TestRateLimits:
    def test_a_runaway_loop_is_stopped(self) -> None:
        policy = permissive()
        allowance = PER_MINUTE[Risk.MEDIUM]
        for _ in range(allowance):
            assert policy.evaluate(request(Risk.MEDIUM)).verdict is not Verdict.DENY
            policy.limiter.record(Risk.MEDIUM)
        denied = policy.evaluate(request(Risk.MEDIUM))
        assert denied.verdict is Verdict.DENY
        assert denied.denial_code == "rate_limited"
        assert denied.retry_after_seconds > 0

    def test_higher_tiers_get_far_smaller_budgets(self) -> None:
        assert PER_MINUTE[Risk.CRITICAL] < PER_MINUTE[Risk.HIGH] < PER_MINUTE[Risk.MEDIUM]
        assert PER_MINUTE[Risk.MEDIUM] < PER_MINUTE[Risk.SAFE]

    def test_one_tier_running_out_does_not_block_another(self) -> None:
        limiter = RateLimiter()
        for _ in range(PER_MINUTE[Risk.CRITICAL]):
            limiter.record(Risk.CRITICAL)
        assert limiter.check(Risk.CRITICAL).allowed is False
        assert limiter.check(Risk.SAFE).allowed is True

    def test_a_limit_is_a_refusal_not_a_queue(self) -> None:
        """Silently delaying what the user asked for would hide the loop."""
        limiter = RateLimiter(per_minute={Risk.HIGH: 1})
        limiter.record(Risk.HIGH)
        verdict = limiter.check(Risk.HIGH)
        assert verdict.allowed is False
        assert "stopped rather than carrying on" in verdict.reason

    def test_prompt_flooding_is_capped(self) -> None:
        """Bury the prompt that matters in forty that do not, and it gets approved."""
        limiter = RateLimiter()
        for _ in range(PROMPTS_PER_MINUTE):
            assert limiter.check_prompt().allowed
            limiter.record_prompt()
        assert limiter.check_prompt().allowed is False

    def test_the_window_slides(self) -> None:
        limiter = RateLimiter(per_minute={Risk.HIGH: 1})
        limiter.record(Risk.HIGH)
        assert limiter.check(Risk.HIGH).allowed is False
        # Rewind the recorded time past the window rather than sleeping 60s.
        limiter._actions[Risk.HIGH][0] -= 61.0
        assert limiter.check(Risk.HIGH).allowed is True


# ── persistence ──────────────────────────────────────────────────────────
class TestPersistence:
    def test_a_revocation_survives_a_restart(self) -> None:
        """A revocation that lasts until the next launch was never a revocation."""
        db = Database(":memory:")
        store = GrantStore(db)
        grants = store.load()
        grants.revoke(Scope.FS_WRITE)
        store.save(grants)
        assert Scope.FS_WRITE not in GrantStore(db).load().granted

    def test_revoking_everything_is_not_mistaken_for_a_fresh_install(self) -> None:
        db = Database(":memory:")
        store = GrantStore(db)
        store.save(ScopeGrants(grants=[]))
        assert GrantStore(db).load().granted == set()

    def test_a_fresh_install_gets_the_defaults(self) -> None:
        assert GrantStore(Database(":memory:")).load().granted == set(DEFAULT_GRANTS)

    def test_an_expired_grant_is_not_written_back(self) -> None:
        db = Database(":memory:")
        store = GrantStore(db)
        grants = store.load()
        grants.grant(Scope.FS_DELETE, ttl_minutes=-1)
        store.save(grants)
        assert Scope.FS_DELETE not in GrantStore(db).load().granted

    def test_a_folder_grant_round_trips(self) -> None:
        db = Database(":memory:")
        store = GrantStore(db)
        grants = store.load()
        grants.revoke(Scope.FS_WRITE)
        grants.grant(Scope.FS_WRITE, "/home/me/Desktop")
        store.save(grants)
        back = GrantStore(db).load()
        assert back.has(Scope.FS_WRITE, "/home/me/Desktop/x")
        assert not back.has(Scope.FS_WRITE, "/home/me/Documents/x")

    def test_the_posture_survives_a_restart(self) -> None:
        """A paused assistant must not quietly return to acting."""
        db = Database(":memory:")
        GrantStore(db).save_posture("paused", True)
        assert GrantStore(db).load_posture() == ("paused", True)

    def test_an_unknown_scope_is_dropped_not_guessed_at(self) -> None:
        db = Database(":memory:")
        store = GrantStore(db)
        store.load()
        db.execute(
            "INSERT INTO scope_grants (scope, target, granted_at) VALUES ('fs.teleport','','t')"
        )
        assert all(isinstance(s, Scope) for s in store.load().granted)


# ── there is no second route to a tool ───────────────────────────────────
class Spy(Tool):
    """A tool that records whether it was executed."""

    def __init__(self, risk: Risk = Risk.HIGH) -> None:
        self.executed = 0
        self._risk = risk

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="spy",
            description="records whether it ran",
            scopes=[Scope.FS_WRITE],
            risk=self._risk,
            input_schema={"type": "object"},
        )

    async def preview(self, args: dict[str, Any]) -> Preview:
        return Preview(
            summary="do the thing",
            affected=1,
            targets=["/home/me/Desktop/x"],
            reversible="permanent",
            # The consent broker refuses to build a prompt without this, which
            # is correct: a tool that cannot say what it would do must not ask.
            blast_radius="Overwrites one file on the Desktop.",
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        self.executed += 1
        return ToolResult(ok=True, summary="did the thing")


class Harness:
    def __init__(self, policy: Policy, *, approve: bool = True) -> None:
        self.db = Database(":memory:")
        self.spy = Spy()
        registry = ToolRegistry()
        registry.register(self.spy)
        self.policy = policy
        self.prompts: list[Any] = []
        self.approve = approve
        self.consent = ConsentBroker(prompt=self._answer, timeout=5)
        self.estop = EmergencyStop()
        self.executor = Executor(
            registry, policy, self.consent, AuditRepository(self.db), self.estop
        )

    async def _answer(self, req: Any) -> None:
        self.prompts.append(req)
        asyncio.get_running_loop().call_soon(
            self.consent.resolve, req.id, ConsentAnswer(approved=self.approve, remember="no")
        )

    async def run(self) -> list[Any]:
        return [e async for e in self.executor.run("spy", {}, origin="red team")]


class TestNoSecondRoute:
    def test_the_executor_is_the_only_caller_of_execute(self) -> None:
        """Asserted on the source: a second route would be invisible otherwise.

        The agent layer must reach `Tool.execute` through the executor and
        nowhere else. If this starts failing, a bypass has been added.
        """
        agents = Path(__file__).resolve().parents[1] / "jarvis" / "agents"
        callers = []
        for path in agents.rglob("*.py"):
            text = path.read_text("utf-8")
            if ".execute(" in text:
                callers.append(path.name)
        assert callers == ["executor.py"], f"something else calls execute: {callers}"

    def test_the_executor_checks_the_policy_before_executing(self) -> None:
        source = inspect.getsource(Executor.run)
        assert source.index("policy.evaluate") < source.index("await tool.execute")

    async def test_a_denied_action_never_reaches_the_tool(self) -> None:
        harness = Harness(Policy(ScopeGrants.of(set())))
        await harness.run()
        assert harness.spy.executed == 0

    async def test_a_declined_confirmation_never_reaches_the_tool(self) -> None:
        harness = Harness(Policy(ScopeGrants.of(set(Scope))), approve=False)
        await harness.run()
        assert harness.spy.executed == 0
        assert harness.prompts, "a high-risk action must have asked"

    async def test_the_emergency_stop_beats_an_approval(self) -> None:
        harness = Harness(Policy(ScopeGrants.of(set(Scope))))
        harness.estop.engage("red team")
        await harness.run()
        assert harness.spy.executed == 0

    async def test_read_only_never_reaches_the_tool_but_still_answers(self) -> None:
        harness = Harness(Policy(ScopeGrants.of(set(Scope)), read_only=True))
        events = await harness.run()
        assert harness.spy.executed == 0
        results = [e for e in events if e.type is EventType.TOOL_RESULT]
        assert results and results[0].data["dryRun"] is True
        assert results[0].data["summary"].startswith("Would ")

    async def test_a_rate_limited_action_never_reaches_the_tool(self) -> None:
        policy = Policy(ScopeGrants.of(set(Scope)), limiter=RateLimiter(per_minute={Risk.HIGH: 0}))
        harness = Harness(policy)
        await harness.run()
        assert harness.spy.executed == 0

    async def test_an_approved_action_does_reach_the_tool(self) -> None:
        """The gate must also let legitimate work through."""
        harness = Harness(Policy(ScopeGrants.of(set(Scope))))
        await harness.run()
        assert harness.spy.executed == 1

    async def test_the_prompt_budget_stops_a_flood_before_asking(self) -> None:
        policy = Policy(ScopeGrants.of(set(Scope)))
        for _ in range(PROMPTS_PER_MINUTE):
            policy.limiter.record_prompt()
        harness = Harness(policy)
        events = await harness.run()
        assert harness.prompts == [], "it must not ask once the budget is gone"
        assert harness.spy.executed == 0
        notices = [e for e in events if e.type is EventType.NOTICE]
        assert notices and notices[0].data["denialCode"] == "prompt_rate_limited"


# ── the whole surface ────────────────────────────────────────────────────
class TestEveryToolIsGated:
    async def test_every_registered_tool_declares_scopes_and_a_tier(self, ctx) -> None:
        for spec in ctx.registry.specs():
            assert spec["scopes"], f"{spec['name']} declares no scopes"
            assert spec["risk"], f"{spec['name']} declares no risk tier"

    async def test_no_tool_declares_a_scope_that_does_not_exist(self, ctx) -> None:
        known = {s.value for s in Scope}
        for spec in ctx.registry.specs():
            unknown = set(spec["scopes"]) - known
            assert not unknown, f"{spec['name']} wants {unknown}"

    async def test_mutating_tools_do_not_run_on_the_install_default(self, ctx) -> None:
        """A fresh install must not be able to delete, shell out or listen."""
        policy = Policy(ScopeGrants())
        for scope in (Scope.FS_DELETE, Scope.SHELL_POWERSHELL, Scope.MIC_LISTEN):
            decision = policy.evaluate(request(Risk.MEDIUM, [scope]))
            assert decision.verdict is Verdict.DENY

    async def test_path_scoped_tools_pass_their_targets_to_the_gate(self, ctx) -> None:
        """Without targets, a folder-limited grant could not be enforced."""
        tool = ctx.registry.get("filesystem")
        preview = await tool.preview({"operation": "list", "path": "desktop"})
        action = tool.action_request({"operation": "list", "path": "desktop"}, preview)
        if set(action.scopes) & PATH_SCOPES:
            assert action.targets, "a path-scoped tool must declare what it touches"

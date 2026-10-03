"""The policy engine. Three axes, all of which must permit an action."""

from __future__ import annotations

import pytest

from jarvis.governance.policy import (
    BULK_THRESHOLD,
    CRITICAL_BULK_THRESHOLD,
    ActionRequest,
    Policy,
    Verdict,
)
from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope, ScopeGrants


def req(
    risk: Risk,
    scopes: list[Scope] | None = None,
    affected: int = 1,
    action: str = "do",
) -> ActionRequest:
    return ActionRequest("test", action, risk, scopes or [Scope.FS_READ], affected=affected)


def all_scopes() -> ScopeGrants:
    return ScopeGrants.of(set(Scope))


class TestModes:
    @pytest.mark.parametrize(
        ("mode", "risk", "expected"),
        [
            ("guarded", Risk.SAFE, Verdict.ALLOW),
            ("guarded", Risk.LOW, Verdict.ALLOW),
            ("guarded", Risk.MEDIUM, Verdict.CONFIRM),
            ("guarded", Risk.HIGH, Verdict.CONFIRM),
            ("assisted", Risk.MEDIUM, Verdict.ALLOW),
            ("assisted", Risk.HIGH, Verdict.CONFIRM),
            ("developer", Risk.MEDIUM, Verdict.ALLOW),
        ],
    )
    def test_ceiling_per_mode(self, mode: str, risk: Risk, expected: Verdict) -> None:
        policy = Policy(all_scopes(), mode=mode)
        assert policy.evaluate(req(risk)).verdict is expected

    def test_paused_executes_nothing_beyond_inspection(self) -> None:
        policy = Policy(all_scopes(), mode="paused")
        assert policy.evaluate(req(Risk.SAFE)).verdict is Verdict.ALLOW
        denial = policy.evaluate(req(Risk.LOW))
        assert denial.verdict is Verdict.DENY
        assert denial.denial_code == "paused"

    def test_read_only_blocks_every_mutation(self) -> None:
        policy = Policy(all_scopes(), mode="assisted", read_only=True)
        assert policy.evaluate(req(Risk.SAFE)).verdict is Verdict.ALLOW
        assert policy.evaluate(req(Risk.LOW)).denial_code == "read_only"


class TestCritical:
    """Tier 5 can never be auto-approved, by any mode or setting."""

    @pytest.mark.parametrize("mode", ["guarded", "assisted", "developer"])
    def test_always_confirms(self, mode: str) -> None:
        policy = Policy(all_scopes(), mode=mode)
        decision = policy.evaluate(req(Risk.CRITICAL))
        assert decision.verdict is Verdict.CONFIRM

    def test_never_offers_remember(self) -> None:
        decision = Policy(all_scopes()).evaluate(req(Risk.CRITICAL))
        assert decision.allow_remember is False

    def test_requires_a_typed_phrase(self) -> None:
        policy = Policy(all_scopes())
        assert policy.evaluate(req(Risk.CRITICAL, action="delete")).confirm_phrase == "DELETE"
        assert policy.evaluate(req(Risk.CRITICAL, action="overwrite")).confirm_phrase == "OVERWRITE"

    def test_cannot_be_remembered_even_if_asked(self) -> None:
        policy = Policy(all_scopes())
        request = req(Risk.CRITICAL)
        policy.remember(request)
        assert policy.evaluate(request).verdict is Verdict.CONFIRM


class TestScopes:
    def test_missing_scope_denies_rather_than_prompting(self) -> None:
        """Permissions are granted deliberately, not under time pressure."""
        policy = Policy(ScopeGrants.of({Scope.FS_READ}))
        decision = policy.evaluate(req(Risk.SAFE, [Scope.FS_DELETE]))
        assert decision.verdict is Verdict.DENY
        assert decision.denial_code == "missing_scope"
        assert Scope.FS_DELETE in decision.missing_scopes

    def test_denial_names_the_missing_permission_in_plain_words(self) -> None:
        policy = Policy(ScopeGrants.of(set()))
        decision = policy.evaluate(req(Risk.SAFE, [Scope.FS_DELETE]))
        assert "Delete files" in decision.reason
        assert "Privacy settings" in decision.reason

    def test_delete_is_not_granted_by_default(self) -> None:
        assert Policy().evaluate(req(Risk.MEDIUM, [Scope.FS_DELETE])).verdict is Verdict.DENY

    def test_scope_check_precedes_the_risk_check(self) -> None:
        """A safe action still needs its scope."""
        policy = Policy(ScopeGrants.of(set()), mode="developer")
        assert policy.evaluate(req(Risk.SAFE, [Scope.FS_READ])).denial_code == "missing_scope"


class TestBulkEscalation:
    """Deleting 4000 files deserves more friction than deleting one."""

    def test_bulk_raises_the_tier(self) -> None:
        policy = Policy(all_scopes(), mode="assisted")
        single = policy.evaluate(req(Risk.MEDIUM, affected=1))
        bulk = policy.evaluate(req(Risk.MEDIUM, affected=BULK_THRESHOLD))
        assert single.verdict is Verdict.ALLOW
        assert bulk.verdict is Verdict.CONFIRM
        assert bulk.risk is Risk.HIGH

    def test_very_large_batches_become_critical(self) -> None:
        decision = Policy(all_scopes()).evaluate(req(Risk.MEDIUM, affected=CRITICAL_BULK_THRESHOLD))
        assert decision.risk is Risk.CRITICAL
        assert decision.allow_remember is False
        assert decision.confirm_phrase

    def test_escalation_withdraws_remember(self) -> None:
        policy = Policy(all_scopes())
        assert policy.evaluate(req(Risk.MEDIUM, affected=1)).allow_remember is True
        assert policy.evaluate(req(Risk.MEDIUM, affected=BULK_THRESHOLD)).allow_remember is False

    def test_the_reason_explains_the_escalation(self) -> None:
        decision = Policy(all_scopes()).evaluate(req(Risk.MEDIUM, affected=40))
        assert "40 items" in decision.reason

    def test_safe_operations_are_not_escalated(self) -> None:
        """Listing 10,000 files is still just listing."""
        decision = Policy(all_scopes()).evaluate(req(Risk.SAFE, affected=10_000))
        assert decision.verdict is Verdict.ALLOW


class TestRemembering:
    def test_a_remembered_medium_action_stops_prompting(self) -> None:
        policy = Policy(all_scopes())
        request = req(Risk.MEDIUM)
        assert policy.evaluate(request).verdict is Verdict.CONFIRM
        policy.remember(request)
        assert policy.evaluate(request).verdict is Verdict.ALLOW

    def test_remembering_is_per_action_not_global(self) -> None:
        policy = Policy(all_scopes())
        policy.remember(req(Risk.MEDIUM, action="move"))
        assert policy.evaluate(req(Risk.MEDIUM, action="delete")).verdict is Verdict.CONFIRM

    def test_high_risk_is_never_remembered(self) -> None:
        policy = Policy(all_scopes())
        request = req(Risk.HIGH)
        policy.remember(request)
        assert policy.evaluate(request).verdict is Verdict.CONFIRM

    def test_forget_all_clears_approvals(self) -> None:
        policy = Policy(all_scopes())
        request = req(Risk.MEDIUM)
        policy.remember(request)
        policy.forget_all()
        assert policy.evaluate(request).verdict is Verdict.CONFIRM


class TestEmergencyStop:
    def test_beats_everything(self) -> None:
        policy = Policy(all_scopes(), mode="developer")
        decision = policy.evaluate(req(Risk.SAFE), stopped=True)
        assert decision.verdict is Verdict.DENY
        assert decision.denial_code == "emergency_stop"

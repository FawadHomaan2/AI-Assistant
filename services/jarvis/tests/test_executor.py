"""Executor: the single path from intent to action.

These are the tests that matter most for the safety claim. If the gate can be
bypassed, everything else in the permission model is decoration.
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.agents.executor import Executor
from jarvis.agents.types import EventType
from jarvis.config import folders
from jarvis.db.engine import Database
from jarvis.db.repositories import AuditRepository
from jarvis.governance.consent import ConsentAnswer, ConsentBroker
from jarvis.governance.estop import EmergencyStop
from jarvis.governance.pathjail import PathJail
from jarvis.governance.policy import Policy
from jarvis.governance.scopes import Scope, ScopeGrants
from jarvis.tools.filesystem import FileSystemTool
from jarvis.tools.registry import ToolRegistry


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME_OVERRIDE", str(tmp_path))
    folders.reset_cache()
    desk = tmp_path / "Desktop"
    desk.mkdir()
    yield desk
    folders.reset_cache()


class Harness:
    """An executor wired to an auto-answering consent broker."""

    def __init__(
        self,
        workspace,
        *,
        scopes: set[Scope],
        mode: str = "guarded",
        approve: bool = True,
    ):
        self.db = Database(":memory:")
        self.audit = AuditRepository(self.db)
        self.estop = EmergencyStop()
        self.policy = Policy(ScopeGrants(granted=scopes), mode=mode)
        self.prompts: list = []
        self.approve = approve
        self.remember = "no"

        self.consent = ConsentBroker(prompt=self._answer, timeout=5)
        registry = ToolRegistry()
        registry.register(FileSystemTool(PathJail([workspace])))
        self.executor = Executor(registry, self.policy, self.consent, self.audit, self.estop)

    async def _answer(self, request) -> None:
        self.prompts.append(request)
        # Answer on the next loop tick, as a real UI would.
        asyncio.get_running_loop().call_soon(
            self.consent.resolve,
            request.id,
            ConsentAnswer(approved=self.approve, remember=self.remember),
        )

    async def run(self, args: dict, tool: str = "filesystem") -> list:
        return [
            e async for e in self.executor.run(tool, args, origin="test", session_id="ses_test")
        ]

    def close(self) -> None:
        self.db.close()


def kinds(events) -> list[EventType]:
    return [e.type for e in events]


def result_of(events):
    return next((e for e in events if e.type is EventType.TOOL_RESULT), None)


def notice_of(events):
    return next((e for e in events if e.type is EventType.NOTICE), None)


class TestAutoAllowed:
    async def test_safe_action_runs_without_a_prompt(self, workspace) -> None:
        h = Harness(workspace, scopes={Scope.FS_READ})
        (workspace / "a.txt").write_text("x")
        events = await h.run({"operation": "list", "path": str(workspace)})
        assert h.prompts == []
        assert result_of(events).data["ok"] is True
        h.close()

    async def test_low_risk_runs_in_guarded_mode(self, workspace) -> None:
        h = Harness(workspace, scopes={Scope.FS_WRITE})
        events = await h.run({"operation": "create_folder", "path": str(workspace / "New")})
        assert h.prompts == []
        assert (workspace / "New").is_dir()
        assert result_of(events).data["ok"] is True
        h.close()


class TestConsentIsRequired:
    async def test_medium_risk_prompts_before_acting(self, workspace) -> None:
        h = Harness(workspace, scopes={Scope.FS_WRITE, Scope.FS_READ})
        (workspace / "a.txt").write_text("x")
        (workspace / "sub").mkdir()
        events = await h.run(
            {
                "operation": "move",
                "path": str(workspace / "a.txt"),
                "destination": str(workspace / "sub"),
            }
        )
        assert len(h.prompts) == 1
        assert result_of(events).data["ok"] is True
        h.close()

    async def test_declining_means_nothing_happens(self, workspace) -> None:
        h = Harness(workspace, scopes={Scope.FS_WRITE, Scope.FS_READ}, approve=False)
        source = workspace / "a.txt"
        source.write_text("original")
        (workspace / "sub").mkdir()
        events = await h.run(
            {"operation": "move", "path": str(source), "destination": str(workspace / "sub")}
        )
        assert source.read_text() == "original", "the file must be untouched"
        assert result_of(events) is None, "no result event for an action that never ran"
        assert "declined" in notice_of(events).data["message"].lower()
        h.close()

    async def test_the_prompt_states_what_where_why_and_reversibility(self, workspace) -> None:
        """The consent contract from ARCHITECTURE section 7."""
        h = Harness(workspace, scopes={Scope.FS_WRITE, Scope.FS_READ})
        (workspace / "a.txt").write_text("x")
        (workspace / "sub").mkdir()
        await h.run(
            {
                "operation": "move",
                "path": str(workspace / "a.txt"),
                "destination": str(workspace / "sub"),
            }
        )
        prompt = h.prompts[0]
        assert prompt.title
        assert prompt.origin == "test"
        assert prompt.targets
        assert prompt.affected_count >= 1
        assert prompt.reversible in {"recycle-bin", "undoable", "permanent", "unknown"}
        assert prompt.blast_radius
        h.close()

    async def test_remembering_skips_the_second_prompt(self, workspace) -> None:
        h = Harness(workspace, scopes={Scope.FS_WRITE, Scope.FS_READ})
        h.remember = "session"
        (workspace / "sub").mkdir()
        for name in ("a.txt", "b.txt"):
            (workspace / name).write_text("x")
            await h.run(
                {
                    "operation": "move",
                    "path": str(workspace / name),
                    "destination": str(workspace / "sub"),
                }
            )
        assert len(h.prompts) == 1, "the second move should reuse the remembered approval"
        h.close()

    async def test_critical_actions_cannot_be_remembered(self, workspace) -> None:
        h = Harness(workspace, scopes={Scope.FS_DELETE})
        h.remember = "always"
        for name in ("a.txt", "b.txt"):
            (workspace / name).write_text("x")
            await h.run({"operation": "delete_permanently", "path": str(workspace / name)})
        assert len(h.prompts) == 2, "a permanent delete must ask every single time"
        h.close()


class TestDenials:
    async def test_missing_scope_blocks_without_prompting(self, workspace) -> None:
        h = Harness(workspace, scopes={Scope.FS_READ})
        target = workspace / "a.txt"
        target.write_text("x")
        events = await h.run({"operation": "delete", "path": str(target)})
        assert h.prompts == [], "a missing permission is not a question"
        assert target.exists()
        assert notice_of(events).data["denialCode"] == "missing_scope"
        assert "fs.delete" in notice_of(events).data["missingScopes"]
        h.close()

    async def test_paused_mode_blocks_mutations(self, workspace) -> None:
        h = Harness(workspace, scopes=set(Scope), mode="paused")
        events = await h.run({"operation": "create_folder", "path": str(workspace / "New")})
        assert not (workspace / "New").exists()
        assert notice_of(events).data["denialCode"] == "paused"
        h.close()

    async def test_path_outside_the_jail_is_refused(self, workspace) -> None:
        h = Harness(workspace, scopes=set(Scope))
        events = await h.run({"operation": "read", "path": "/etc/passwd"})
        error = next(e for e in events if e.type is EventType.ERROR)
        assert error.data["code"] == "jarvis.path.denied"
        h.close()

    async def test_emergency_stop_blocks_before_anything_runs(self, workspace) -> None:
        h = Harness(workspace, scopes=set(Scope))
        h.estop.engage("test")
        events = await h.run({"operation": "create_folder", "path": str(workspace / "New")})
        assert not (workspace / "New").exists()
        assert events[0].type is EventType.ERROR
        assert events[0].data["code"] == "jarvis.emergency_stop"
        h.close()


class TestVerification:
    """Success is measured, not assumed."""

    async def test_result_is_marked_verified(self, workspace) -> None:
        h = Harness(workspace, scopes={Scope.FS_WRITE})
        events = await h.run({"operation": "create_folder", "path": str(workspace / "New")})
        assert result_of(events).data["verified"] is True
        h.close()

    async def test_a_tool_that_lies_is_caught(self, workspace, monkeypatch) -> None:
        """If the observed state does not match the claim, it is not a success."""
        h = Harness(workspace, scopes={Scope.FS_WRITE})
        tool = h.executor.registry.get("filesystem")

        async def claim_without_doing(args):
            from jarvis.tools.base import ToolResult

            return ToolResult(ok=True, summary="Created folder New", changes=["Created New"])

        monkeypatch.setattr(tool, "execute", claim_without_doing)
        events = await h.run({"operation": "create_folder", "path": str(workspace / "New")})
        result = result_of(events)
        assert result.data["verified"] is False
        assert result.data["ok"] is False, "an unverified claim must not be reported as success"
        h.close()


class TestAudit:
    async def test_every_outcome_is_recorded(self, workspace) -> None:
        h = Harness(workspace, scopes={Scope.FS_WRITE})
        await h.run({"operation": "create_folder", "path": str(workspace / "New")})
        entries = h.audit.recent()
        assert any(e["action"] == "tool.filesystem.create_folder" for e in entries)
        assert h.audit.verify() == (True, None)
        h.close()

    async def test_denials_are_audited_too(self, workspace) -> None:
        h = Harness(workspace, scopes=set())
        await h.run({"operation": "create_folder", "path": str(workspace / "New")})
        assert any(e["outcome"] == "denied" for e in h.audit.recent())
        h.close()

    async def test_arguments_are_digested_not_stored(self, workspace) -> None:
        h = Harness(workspace, scopes={Scope.FS_WRITE})
        secret_name = "my-very-distinctive-folder-name"
        await h.run({"operation": "create_folder", "path": str(workspace / secret_name)})
        assert all(secret_name not in str(e) for e in h.audit.recent())
        h.close()

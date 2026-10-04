"""ProcessTool, ApplicationTool and WindowTool.

The process layer runs against real psutil, so most of it is genuinely verified.
Launching and window control use fake backends: the tool logic is tested here,
the Win32 calls themselves need a Windows machine.
"""

from __future__ import annotations

import os

import psutil
import pytest

from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.platform_.base import AppBackend, ProcessBackend, WindowBackend
from jarvis.platform_.posix import PosixWindowBackend, PsutilProcessBackend
from jarvis.platform_.types import AppInfo, LaunchResult, ProcessInfo, WindowInfo
from jarvis.tools.applications import AppAmbiguous, ApplicationTool, AppNotFound
from jarvis.tools.base import ToolError, ToolInputInvalid
from jarvis.tools.processes import ProcessTool
from jarvis.tools.windows_tool import WindowTool
from jarvis.util.errors import PlatformUnsupported


# ── fakes ────────────────────────────────────────────────────────────────
class FakeProcesses(ProcessBackend):
    def __init__(self, procs: list[ProcessInfo]) -> None:
        self.procs = procs
        self.terminated: list[tuple[int, bool]] = []

    def list_all(self, *, limit: int = 500) -> list[ProcessInfo]:
        return self.procs[:limit]

    def get(self, pid: int) -> ProcessInfo | None:
        return next((p for p in self.procs if p.pid == pid), None)

    def find(self, name: str) -> list[ProcessInfo]:
        wanted = name.lower().removesuffix(".exe")
        return [p for p in self.procs if wanted in p.name.lower().removesuffix(".exe")]

    def terminate(self, pid: int, *, force: bool = False, timeout: float = 5.0) -> bool:
        self.terminated.append((pid, force))
        self.procs = [p for p in self.procs if p.pid != pid]
        return True


class FakeApps(AppBackend):
    def __init__(self, apps: list[AppInfo]) -> None:
        self.apps = apps
        self.launched: list[str] = []

    def catalogue(self, *, refresh: bool = False) -> list[AppInfo]:
        return self.apps

    def launch(self, app: AppInfo, *, arguments: list[str] | None = None) -> LaunchResult:
        self.launched.append(app.launch_target)
        return LaunchResult(app=app, pid=4242, detail="fake launch")

    def describe(self) -> str:
        return "fake backend"


class FakeWindows(WindowBackend):
    def __init__(self, windows: list[WindowInfo]) -> None:
        self.windows = windows
        self.calls: list[tuple[str, int]] = []

    def list_all(self, *, visible_only: bool = True) -> list[WindowInfo]:
        return [w for w in self.windows if w.visible or not visible_only]

    def foreground(self) -> WindowInfo | None:
        return next((w for w in self.windows if w.foreground), None)

    def _record(self, action: str, handle: int) -> bool:
        self.calls.append((action, handle))
        return True

    def focus(self, handle: int) -> bool:
        return self._record("focus", handle)

    def minimise(self, handle: int) -> bool:
        return self._record("minimise", handle)

    def maximise(self, handle: int) -> bool:
        return self._record("maximise", handle)

    def restore(self, handle: int) -> bool:
        return self._record("restore", handle)

    def close(self, handle: int) -> bool:
        return self._record("close", handle)


def proc(pid: int, name: str, **kw) -> ProcessInfo:
    from jarvis.platform_.protected import check

    protected, reason = check(pid, name, kw.get("exe", ""))
    return ProcessInfo(pid=pid, name=name, protected=protected, protection_reason=reason, **kw)


# ── ProcessTool ──────────────────────────────────────────────────────────
class TestProcessToolAgainstRealPsutil:
    """psutil is the same on Windows and here, so this is really verified."""

    @pytest.fixture
    def tool(self) -> ProcessTool:
        return ProcessTool(PsutilProcessBackend())

    async def test_lists_real_processes(self, tool) -> None:
        result = await tool.execute({"operation": "list", "limit": 10})
        procs = result.data["processes"]
        assert procs
        assert all(p["pid"] > 0 for p in procs)
        assert any(p["memoryBytes"] > 0 for p in procs)

    async def test_finds_this_process(self, tool) -> None:
        """Search by the name this process actually has, not an assumed one.

        It used to search for "python", which located this process only when
        the suite was started as `python -m pytest`. CI runs `pytest` directly,
        where the process is named `pytest`, and the assertion failed for a
        reason that had nothing to do with the code under test.
        """
        name = psutil.Process(os.getpid()).name()
        result = await tool.execute({"operation": "find", "name": name})
        assert any(p["pid"] == os.getpid() for p in result.data["processes"])

    async def test_info_for_a_known_pid(self, tool) -> None:
        result = await tool.execute({"operation": "info", "pid": os.getpid()})
        assert result.data["pid"] == os.getpid()
        assert result.data["protected"] is True  # it is Jarvis itself

    async def test_info_for_a_missing_pid_errors(self, tool) -> None:
        with pytest.raises(ToolError, match="No process with pid"):
            await tool.execute({"operation": "info", "pid": 999_999})

    # A single sample would report 0% for everything.
    async def test_top_measures_cpu_over_an_interval(self, tool) -> None:
        result = await tool.execute({"operation": "top", "limit": 5})
        assert result.data["sampleSeconds"] > 0
        assert result.data["processes"]

    async def test_this_process_is_protected_from_itself(self, tool) -> None:
        preview = await tool.preview({"operation": "end", "pid": os.getpid()})
        assert preview.blocked
        assert "Jarvis itself" in preview.blocked


class TestProcessToolEnding:
    @pytest.fixture
    def backend(self) -> FakeProcesses:
        return FakeProcesses(
            [
                proc(100, "chrome.exe", memory_bytes=500_000_000),
                proc(101, "chrome.exe", memory_bytes=200_000_000),
                proc(200, "csrss.exe"),
                proc(300, "MsMpEng.exe"),
                proc(400, "notepad.exe", memory_bytes=10_000_000),
            ]
        )

    @pytest.fixture
    def tool(self, backend) -> ProcessTool:
        return ProcessTool(backend)

    async def test_ending_needs_the_kill_scope(self, tool) -> None:
        assert Scope.PROCESS_KILL in tool.scopes_for({"operation": "end"})
        assert Scope.PROCESS_KILL not in tool.scopes_for({"operation": "list"})

    # A graceful close lets the program save; a force kill does not.
    async def test_force_is_a_higher_tier_than_a_graceful_close(self, tool) -> None:
        preview = await tool.preview({"operation": "end", "name": "notepad"})
        assert tool.risk_for({"operation": "end"}, preview) is Risk.MEDIUM
        assert tool.risk_for({"operation": "end", "force": True}, preview) is Risk.HIGH

    async def test_preview_counts_every_matching_process(self, tool) -> None:
        preview = await tool.preview({"operation": "end", "name": "chrome"})
        assert preview.affected == 2
        assert "700 MB" in preview.blast_radius or "668" in preview.blast_radius

    async def test_the_prompt_warns_about_unsaved_work_on_a_force_kill(self, tool) -> None:
        preview = await tool.preview({"operation": "end", "name": "notepad", "force": True})
        assert "unsaved work" in preview.blast_radius

    async def test_a_graceful_close_says_the_program_can_prompt(self, tool) -> None:
        preview = await tool.preview({"operation": "end", "name": "notepad"})
        assert "prompt you to save" in preview.blast_radius

    @pytest.mark.parametrize("name", ["csrss", "MsMpEng"])
    async def test_protected_processes_are_blocked_in_preview(self, tool, name: str) -> None:
        preview = await tool.preview({"operation": "end", "name": name})
        assert preview.blocked
        assert "not something a confirmation can override" in preview.blocked

    async def test_protected_processes_are_refused_at_execution_too(self, tool) -> None:
        """Re-checked at the point of action: pids get reused."""
        with pytest.raises(ToolError, match="will not end"):
            await tool.execute({"operation": "end", "name": "csrss"})

    async def test_ending_an_ordinary_program_works(self, tool, backend) -> None:
        result = await tool.execute({"operation": "end", "name": "notepad"})
        assert result.ok
        assert backend.terminated == [(400, False)]
        assert backend.get(400) is None

    async def test_force_is_passed_through(self, tool, backend) -> None:
        await tool.execute({"operation": "end", "name": "notepad", "force": True})
        assert backend.terminated == [(400, True)]

    async def test_nothing_running_is_reported_not_silently_ok(self, tool) -> None:
        preview = await tool.preview({"operation": "end", "name": "absent"})
        assert preview.blocked
        assert "No running program matches" in preview.blocked

    async def test_end_without_a_target_is_rejected(self, tool) -> None:
        with pytest.raises(ToolInputInvalid, match="needs a name or a pid"):
            await tool.preview({"operation": "end"})


# ── ApplicationTool ──────────────────────────────────────────────────────
class TestApplicationTool:
    @pytest.fixture
    def apps(self) -> FakeApps:
        return FakeApps(
            [
                AppInfo("chrome", "Google Chrome", r"C:\chrome.exe"),
                AppInfo("winword", "Microsoft Word", r"C:\winword.exe"),
                AppInfo("wordpad", "WordPad", r"C:\wordpad.exe"),
                AppInfo("code", "Visual Studio Code", r"C:\code.exe"),
                AppInfo("excel", "Microsoft Excel", r"C:\excel.exe"),
                AppInfo("teams", "Microsoft Teams", r"C:\teams.exe"),
            ]
        )

    @pytest.fixture
    def tool(self, apps) -> ApplicationTool:
        return ApplicationTool(apps, FakeProcesses([]))

    async def test_launch_resolves_a_name_to_installed_software(self, tool, apps) -> None:
        result = await tool.execute({"operation": "launch", "name": "chrome"})
        assert result.ok
        assert apps.launched == [r"C:\chrome.exe"]
        assert result.data["controlLayer"] == "L1"

    # The security property of this whole tool.
    def test_there_is_no_path_input(self, tool) -> None:
        """A path input would let a prompt-injected model run any binary."""
        properties = tool.spec.input_schema["properties"]
        assert "path" not in properties
        assert "command" not in properties
        assert "executable" not in properties

    async def test_an_arbitrary_path_cannot_be_launched(self, tool, apps) -> None:
        with pytest.raises(AppNotFound):
            await tool.execute(
                {"operation": "launch", "name": r"C:\Users\me\Downloads\invoice.pdf.exe"}
            )
        assert apps.launched == []

    async def test_uninstalled_software_is_refused_clearly(self, tool) -> None:
        preview = await tool.preview({"operation": "launch", "name": "photoshop"})
        assert preview.blocked
        assert "only starts software it can see is installed" in preview.blocked

    async def test_an_ambiguous_name_asks(self, tool) -> None:
        with pytest.raises(AppAmbiguous, match="Which one did you mean"):
            await tool.execute({"operation": "launch", "name": "microsoft"})

    async def test_launching_is_low_risk_and_listing_is_safe(self, tool) -> None:
        preview = await tool.preview({"operation": "launch", "name": "chrome"})
        assert tool.risk_for({"operation": "launch"}, preview) is Risk.LOW
        assert tool.risk_for({"operation": "list"}, preview) is Risk.SAFE

    async def test_preview_notes_when_already_running(self, apps) -> None:
        tool = ApplicationTool(apps, FakeProcesses([proc(1, "chrome.exe")]))
        preview = await tool.preview({"operation": "launch", "name": "chrome"})
        assert "already running" in preview.blast_radius

    async def test_find_returns_ranked_candidates(self, tool) -> None:
        result = await tool.execute({"operation": "find", "name": "word"})
        assert result.data["matches"][0]["name"] == "Microsoft Word"
        assert result.data["matches"][0]["score"] > 0

    async def test_list_names_its_source(self, tool) -> None:
        result = await tool.execute({"operation": "list"})
        assert result.data["source"] == "fake backend"


# ── WindowTool ───────────────────────────────────────────────────────────
class TestWindowToolOnPosix:
    """Unavailable here, and it says so rather than returning an empty list."""

    @pytest.fixture
    def tool(self) -> WindowTool:
        return WindowTool(PosixWindowBackend())

    def test_the_spec_reports_it_as_unavailable(self, tool) -> None:
        spec = tool.spec
        assert spec.available is False
        assert "Windows API" in spec.unavailable_reason

    async def test_listing_raises_rather_than_returning_nothing(self, tool) -> None:
        """ "You have no windows open" would be a confident wrong answer."""
        with pytest.raises(PlatformUnsupported):
            await tool.execute({"operation": "list"})

    @pytest.mark.parametrize("operation", ["focus", "minimise", "maximise", "close"])
    async def test_every_control_operation_raises(self, tool, operation: str) -> None:
        with pytest.raises(PlatformUnsupported):
            await tool.preview({"operation": operation, "title": "anything"})


class TestWindowToolLogic:
    """Tool behaviour, with a fake backend standing in for Win32."""

    @pytest.fixture
    def backend(self) -> FakeWindows:
        return FakeWindows(
            [
                WindowInfo(1, "Inbox - Outlook", 10, "outlook.exe", foreground=True),
                WindowInfo(2, "report.docx - Word", 20, "winword.exe"),
                WindowInfo(3, "notes.txt - Notepad", 30, "notepad.exe", minimised=True),
                WindowInfo(4, "Hidden", 40, "thing.exe", visible=False),
            ]
        )

    @pytest.fixture
    def tool(self, backend) -> WindowTool:
        return WindowTool(backend)

    async def test_list_excludes_invisible_windows(self, tool) -> None:
        result = await tool.execute({"operation": "list"})
        assert len(result.data["windows"]) == 3

    async def test_foreground_is_identified(self, tool) -> None:
        result = await tool.execute({"operation": "foreground"})
        assert result.data["window"]["title"] == "Inbox - Outlook"

    async def test_a_window_is_found_by_part_of_its_title(self, tool, backend) -> None:
        await tool.execute({"operation": "focus", "title": "notepad"})
        assert backend.calls == [("focus", 3)]

    async def test_a_window_is_found_by_process_name(self, tool, backend) -> None:
        await tool.execute({"operation": "minimise", "title": "winword"})
        assert backend.calls == [("minimise", 2)]

    async def test_an_ambiguous_title_asks_rather_than_guessing(self, tool) -> None:
        backend = FakeWindows(
            [
                WindowInfo(1, "a - Word", 1, "winword.exe"),
                WindowInfo(2, "b - Word", 2, "winword.exe"),
            ]
        )
        ambiguous = WindowTool(backend)
        # preview reports it so the UI can show a notice...
        result = await ambiguous.preview({"operation": "focus", "title": "word"})
        assert result.blocked
        assert "Which one did you mean" in result.blocked
        # ...and execute refuses rather than picking one.
        with pytest.raises(ToolError, match="Which one did you mean"):
            await ambiguous.execute({"operation": "focus", "title": "word"})
        assert backend.calls == []

    async def test_a_missing_window_is_reported(self, tool) -> None:
        preview = await tool.preview({"operation": "focus", "title": "nothing-like-this"})
        assert preview.blocked
        assert "No open window matches" in preview.blocked

    # Closing can lose unsaved work; moving a window cannot.
    async def test_close_is_a_higher_tier_than_minimise(self, tool) -> None:
        preview = await tool.preview({"operation": "close", "title": "notepad"})
        assert tool.risk_for({"operation": "close"}, preview) is Risk.MEDIUM
        assert tool.risk_for({"operation": "minimise"}, preview) is Risk.LOW

    async def test_the_close_prompt_is_honest_about_unsaved_work(self, tool) -> None:
        preview = await tool.preview({"operation": "close", "title": "notepad"})
        assert "should prompt you" in preview.blast_radius
        assert "that work is lost" in preview.blast_radius
        assert preview.reversible == "unknown"

    async def test_moving_a_window_loses_nothing(self, tool) -> None:
        preview = await tool.preview({"operation": "minimise", "title": "notepad"})
        assert preview.reversible == "undoable"
        assert "Nothing is lost" in preview.blast_radius

    async def test_close_needs_the_app_control_scope(self, tool) -> None:
        assert Scope.APP_CONTROL in tool.scopes_for({"operation": "close"})
        assert Scope.APP_CONTROL not in tool.scopes_for({"operation": "minimise"})

    async def test_listing_only_needs_read(self, tool) -> None:
        assert tool.scopes_for({"operation": "list"}) == [Scope.PROCESS_READ]

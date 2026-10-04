"""Win32 window logic, driven by a fake `user32`.

The real `user32` calls need Windows. What *can* be verified anywhere is the
layer that decides which handles become windows a person would recognise, and
how their state is mapped — which is where the bugs that would make
"which programs are open?" useless actually live.

The Win32 calls themselves (EnumWindows, ShowWindow, SetForegroundWindow,
PostMessage) remain unverified until run on Windows; docs/PHASES.md says so.
"""

from __future__ import annotations

import pytest

from jarvis.platform_ import win32
from jarvis.platform_.win32 import GWL_EXSTYLE, WS_EX_TOOLWINDOW, Win32WindowBackend


class FakeUser32:
    """Stands in for user32, recording the calls the backend makes."""

    def __init__(self, windows: dict[int, dict]) -> None:
        self.windows = windows
        self.foreground_handle = next((h for h, w in windows.items() if w.get("foreground")), 0)
        self.calls: list[tuple[str, int]] = []

    # — queries —
    def GetWindowTextLengthW(self, hwnd):  # noqa: N802 - mirrors the Win32 name
        return len(self.windows.get(int(hwnd), {}).get("title", ""))

    def GetWindowTextW(self, hwnd, buffer, _size):  # noqa: N802
        buffer.value = self.windows.get(int(hwnd), {}).get("title", "")
        return len(buffer.value)

    def IsWindowVisible(self, hwnd):  # noqa: N802
        return int(self.windows.get(int(hwnd), {}).get("visible", True))

    def IsWindow(self, hwnd):  # noqa: N802
        return int(int(hwnd) in self.windows)

    def IsIconic(self, hwnd):  # noqa: N802
        return int(self.windows.get(int(hwnd), {}).get("minimised", False))

    def IsZoomed(self, hwnd):  # noqa: N802
        return int(self.windows.get(int(hwnd), {}).get("maximised", False))

    def GetWindowLongW(self, hwnd, index):  # noqa: N802
        if index == GWL_EXSTYLE:
            return self.windows.get(int(hwnd), {}).get("exstyle", 0)
        return 0

    def GetWindowThreadProcessId(self, hwnd, pid_ptr):  # noqa: N802
        pid = self.windows.get(int(hwnd), {}).get("pid", 0)
        if pid_ptr is not None:
            pid_ptr._obj.value = pid
        return 1234

    def GetWindowRect(self, hwnd, rect_ptr):  # noqa: N802
        bounds = self.windows.get(int(hwnd), {}).get("bounds", (0, 0, 800, 600))
        rect = rect_ptr._obj
        rect.left, rect.top, rect.right, rect.bottom = bounds
        return 1

    def GetForegroundWindow(self):  # noqa: N802
        return self.foreground_handle

    # — actions —
    def ShowWindow(self, hwnd, command):  # noqa: N802
        self.calls.append(("ShowWindow", command))
        return 1

    def SetForegroundWindow(self, hwnd):  # noqa: N802
        self.calls.append(("SetForegroundWindow", int(hwnd)))
        self.foreground_handle = int(hwnd)
        return 1

    def PostMessageW(self, hwnd, message, _w, _l):  # noqa: N802
        self.calls.append(("PostMessageW", message))
        return 1

    def AttachThreadInput(self, _a, _b, attach):  # noqa: N802
        self.calls.append(("AttachThreadInput", int(attach)))
        return 1


class FakeKernel32:
    def GetCurrentThreadId(self):  # noqa: N802
        return 999


@pytest.fixture
def backend(monkeypatch):
    windows = {
        10: {"title": "Inbox - Outlook", "pid": 100, "foreground": True},
        11: {"title": "report.docx - Word", "pid": 200},
        12: {"title": "", "pid": 300},  # no title: not a user-facing window
        13: {"title": "Tooltip", "pid": 400, "exstyle": WS_EX_TOOLWINDOW},
        14: {"title": "Hidden", "pid": 500, "visible": False},
        15: {"title": "Notepad", "pid": 600, "minimised": True},
    }
    user32 = FakeUser32(windows)
    instance = Win32WindowBackend(user32=user32, kernel32=FakeKernel32())
    # The backend asks psutil for process names; keep it deterministic.
    monkeypatch.setattr(
        Win32WindowBackend, "_process_names", staticmethod(lambda: {100: "outlook.exe"})
    )
    # dwmapi is a Windows DLL; nothing is cloaked in these fixtures.
    monkeypatch.setattr(Win32WindowBackend, "_is_cloaked", lambda self, hwnd: False)
    return instance, user32, list(windows)


class TestWindowFiltering:
    """ "Which programs are open?" is useless if it lists helper windows."""

    def test_only_real_application_windows_are_listed(self, backend) -> None:
        instance, _user32, handles = backend
        titles = [w.title for w in instance.build_windows(handles)]
        assert titles == ["Inbox - Outlook", "report.docx - Word", "Notepad"]

    def test_untitled_windows_are_excluded(self, backend) -> None:
        instance, _u, handles = backend
        assert all(w.title for w in instance.build_windows(handles))

    def test_tool_windows_are_excluded(self, backend) -> None:
        instance, _u, handles = backend
        assert "Tooltip" not in [w.title for w in instance.build_windows(handles)]

    def test_invisible_windows_are_excluded(self, backend) -> None:
        instance, _u, handles = backend
        assert "Hidden" not in [w.title for w in instance.build_windows(handles)]

    def test_everything_is_included_when_the_filter_is_off(self, backend) -> None:
        instance, _u, handles = backend
        assert len(instance.build_windows(handles, visible_only=False)) == len(handles)


class TestWindowMapping:
    def test_state_flags_are_mapped(self, backend) -> None:
        instance, _u, handles = backend
        by_title = {w.title: w for w in instance.build_windows(handles)}
        assert by_title["Inbox - Outlook"].foreground is True
        assert by_title["Notepad"].minimised is True
        assert by_title["report.docx - Word"].foreground is False

    def test_pid_and_process_name_are_attached(self, backend) -> None:
        instance, _u, handles = backend
        outlook = next(w for w in instance.build_windows(handles) if w.pid == 100)
        assert outlook.process_name == "outlook.exe"

    def test_bounds_are_read(self, backend) -> None:
        instance, _u, handles = backend
        assert instance.build_windows(handles)[0].bounds == (0, 0, 800, 600)


class TestWindowActions:
    def test_minimise_sends_the_right_command(self, backend) -> None:
        instance, user32, _ = backend
        instance.minimise(10)
        assert ("ShowWindow", win32.SW_MINIMIZE) in user32.calls

    def test_maximise_sends_the_right_command(self, backend) -> None:
        instance, user32, _ = backend
        instance.maximise(10)
        assert ("ShowWindow", win32.SW_MAXIMIZE) in user32.calls

    def test_close_posts_wm_close_not_a_kill(self, backend) -> None:
        """A request the application can answer, so it can prompt to save."""
        instance, user32, _ = backend
        instance.close(11)
        assert ("PostMessageW", win32.WM_CLOSE) in user32.calls

    def test_focusing_a_minimised_window_restores_it_first(self, backend) -> None:
        instance, user32, _ = backend
        instance.focus(15)  # Notepad is minimised
        assert ("ShowWindow", win32.SW_RESTORE) in user32.calls
        assert ("SetForegroundWindow", 15) in user32.calls

    def test_focus_attaches_and_detaches_the_input_queue(self, backend) -> None:
        """Windows blocks focus stealing unless we attach to the foreground thread."""
        instance, user32, _ = backend
        instance.focus(11)
        attaches = [c for c in user32.calls if c[0] == "AttachThreadInput"]
        assert ("AttachThreadInput", 1) in attaches
        assert ("AttachThreadInput", 0) in attaches, "the attachment must be released"

    def test_acting_on_a_closed_window_is_reported(self, backend) -> None:
        from jarvis.util.errors import JarvisError

        instance, _u, _ = backend
        with pytest.raises(JarvisError, match="no longer exists"):
            instance.minimise(9999)

"""Windows adapters — the real target platform.

Control layer L1 from ARCHITECTURE section 10: native Win32 and the shell, not
screen coordinates. Window handling goes through `user32` with ctypes rather
than pywin32, because the calls are few and well-defined and it removes a heavy
dependency from the critical path; application launching uses `ShellExecuteExW`,
so Jarvis starts a program exactly the way double-clicking it does — which is
what makes shortcuts, file associations and Store apps work.

**This module cannot be executed on anything but Windows.** It is written
against the documented API and its structure is unit-tested through a fake
`user32`, but the real calls need a Windows machine to confirm. That limitation
is recorded in docs/PHASES.md rather than papered over.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from jarvis.platform_.base import AppBackend, WindowBackend
from jarvis.platform_.posix import PsutilProcessBackend
from jarvis.platform_.types import AppInfo, LaunchResult, WindowInfo
from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger

log = get_logger(__name__)

# ── ShowWindow commands ─────────────────────────────────────────────────────
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_MAXIMIZE = 3
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

WM_CLOSE = 0x0010

# ── ShellExecuteEx flags ────────────────────────────────────────────────────
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_FLAG_NO_UI = 0x00000400

#: Window styles used to tell a real application window from a tool window.
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080

#: Cloaked UWP windows report as visible but are not on screen.
DWMWA_CLOAKED = 14


def _user32() -> Any:
    """The user32 handle, with argument types declared.

    Declaring argtypes matters on 64-bit: an undeclared HWND is truncated to a
    C int, which silently targets the wrong window.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    user32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM,
    ]
    user32.EnumWindows.restype = wintypes.BOOL

    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int

    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.IsZoomed.restype = wintypes.BOOL

    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL

    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL

    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND

    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL

    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL

    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG

    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND

    return user32


class Win32WindowBackend(WindowBackend):
    """Top-level window enumeration and control."""

    def __init__(self, user32: Any = None, kernel32: Any = None) -> None:
        # Injectable so the enumeration and filtering logic can be tested with a
        # fake user32 on a non-Windows machine.
        self._user32 = user32 if user32 is not None else _user32()
        self._kernel32 = (
            kernel32 if kernel32 is not None else ctypes.WinDLL("kernel32", use_last_error=True)
        )
        self._dwmapi: Any = None

    # ── helpers ─────────────────────────────────────────────────────────
    def _title(self, hwnd: int) -> str:
        length = self._user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def _pid(self, hwnd: int) -> int:
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _bounds(self, hwnd: int) -> tuple[int, int, int, int]:
        rect = wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return (0, 0, 0, 0)
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

    def _is_cloaked(self, hwnd: int) -> bool:
        """UWP windows linger as visible-but-cloaked after being closed."""
        try:
            if self._dwmapi is None:
                self._dwmapi = ctypes.WinDLL("dwmapi")
            cloaked = ctypes.c_int(0)
            self._dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(DWMWA_CLOAKED),
                ctypes.byref(cloaked),
                ctypes.sizeof(cloaked),
            )
            return bool(cloaked.value)
        except Exception:  # dwmapi is unavailable on some SKUs
            return False

    def _is_app_window(self, hwnd: int) -> bool:
        """Filter to windows a person would recognise as an open application.

        Without this the list is dominated by invisible helper windows, which
        makes "which programs are open?" useless.
        """
        if not self._user32.IsWindowVisible(hwnd):
            return False
        if not self._title(hwnd):
            return False
        style = self._user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if style & WS_EX_TOOLWINDOW:
            return False
        return not self._is_cloaked(hwnd)

    # ── interface ───────────────────────────────────────────────────────
    def _enumerate_handles(self) -> list[int]:
        """Collect every top-level window handle.

        Separated from `build_windows` because `WINFUNCTYPE` exists only on
        Windows, and keeping the callback here lets the filtering and mapping
        below be tested on any platform with a fake user32.
        """
        handles: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect(hwnd: int, _lparam: int) -> bool:
            handles.append(int(hwnd))
            return True  # keep enumerating

        self._user32.EnumWindows(collect, 0)
        return handles

    def list_all(self, *, visible_only: bool = True) -> list[WindowInfo]:
        return self.build_windows(self._enumerate_handles(), visible_only=visible_only)

    def build_windows(self, handles: list[int], *, visible_only: bool = True) -> list[WindowInfo]:
        """Turn raw handles into the window list, applying the app-window filter."""
        foreground = int(self._user32.GetForegroundWindow() or 0)
        names = self._process_names()

        out: list[WindowInfo] = []
        for hwnd in handles:
            if visible_only and not self._is_app_window(hwnd):
                continue
            pid = self._pid(hwnd)
            out.append(
                WindowInfo(
                    handle=hwnd,
                    title=self._title(hwnd),
                    pid=pid,
                    process_name=names.get(pid, ""),
                    visible=bool(self._user32.IsWindowVisible(hwnd)),
                    minimised=bool(self._user32.IsIconic(hwnd)),
                    maximised=bool(self._user32.IsZoomed(hwnd)),
                    foreground=hwnd == foreground,
                    bounds=self._bounds(hwnd),
                )
            )
        return out

    @staticmethod
    def _process_names() -> dict[int, str]:
        import psutil

        names: dict[int, str] = {}
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                names[proc.info["pid"]] = proc.info["name"] or ""
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return names

    def foreground(self) -> WindowInfo | None:
        hwnd = int(self._user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        return next((w for w in self.list_all(visible_only=False) if w.handle == hwnd), None)

    def _require_window(self, handle: int) -> None:
        if not self._user32.IsWindow(handle):
            raise JarvisError(
                f"Window {handle} no longer exists — it was probably closed.",
                handle=handle,
            )

    def focus(self, handle: int) -> bool:
        """Bring a window forward.

        Windows refuses `SetForegroundWindow` from a process that does not own
        the current foreground window, to stop applications stealing focus. The
        documented way round it is to attach to the foreground thread's input
        queue for the duration of the call — which is what a user clicking the
        taskbar effectively does.
        """
        self._require_window(handle)
        if self._user32.IsIconic(handle):
            self._user32.ShowWindow(handle, SW_RESTORE)

        current = self._user32.GetForegroundWindow()
        if not current:
            return bool(self._user32.SetForegroundWindow(handle))

        our_thread = self._kernel32.GetCurrentThreadId()
        their_thread = self._user32.GetWindowThreadProcessId(current, None)
        attached = False
        if their_thread and their_thread != our_thread:
            attached = bool(self._user32.AttachThreadInput(our_thread, their_thread, True))
        try:
            return bool(self._user32.SetForegroundWindow(handle))
        finally:
            if attached:
                self._user32.AttachThreadInput(our_thread, their_thread, False)

    def minimise(self, handle: int) -> bool:
        self._require_window(handle)
        return bool(self._user32.ShowWindow(handle, SW_MINIMIZE))

    def maximise(self, handle: int) -> bool:
        self._require_window(handle)
        return bool(self._user32.ShowWindow(handle, SW_MAXIMIZE))

    def restore(self, handle: int) -> bool:
        self._require_window(handle)
        return bool(self._user32.ShowWindow(handle, SW_RESTORE))

    def close(self, handle: int) -> bool:
        """Post WM_CLOSE — the same message the window's X button sends.

        The application decides what to do, including prompting about unsaved
        work. It is a request, not a kill, which is why this is a lower risk
        tier than terminating the process.
        """
        self._require_window(handle)
        return bool(self._user32.PostMessageW(handle, WM_CLOSE, 0, 0))


class Win32AppBackend(AppBackend):
    """Installed applications, from the registry and the Start Menu."""

    def __init__(self) -> None:
        self._cache: list[AppInfo] | None = None

    def describe(self) -> str:
        return "App Paths registry + Start Menu shortcuts, launched via ShellExecuteExW"

    def _from_registry(self) -> list[AppInfo]:
        """`App Paths` is the registry of programs that can be started by name."""
        import winreg

        found: list[AppInfo] = []
        key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, key_path) as parent:
                    index = 0
                    while True:
                        try:
                            name = winreg.EnumKey(parent, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            with winreg.OpenKey(parent, name) as entry:
                                target, _ = winreg.QueryValueEx(entry, "")
                        except OSError:
                            continue
                        if not target:
                            continue
                        found.append(
                            AppInfo(
                                key=name.lower().removesuffix(".exe"),
                                name=Path(str(target)).stem.replace("_", " ").title(),
                                launch_target=str(target).strip('"'),
                                source="app-paths",
                                aliases=[name.lower().removesuffix(".exe")],
                            )
                        )
            except OSError:
                continue
        return found

    def _from_start_menu(self) -> list[AppInfo]:
        """Shortcuts are launched as-is; the shell resolves them.

        Launching the `.lnk` rather than its target means no shortcut parsing,
        and arguments and working directories baked into the shortcut keep
        working.
        """
        import os

        roots = [
            Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        ]
        found: list[AppInfo] = []
        for root in roots:
            if not root.is_dir():
                continue
            for link in root.rglob("*.lnk"):
                if "uninstall" in link.stem.lower():
                    continue
                found.append(
                    AppInfo(
                        key=link.stem.lower(),
                        name=link.stem,
                        launch_target=str(link),
                        source="start-menu",
                    )
                )
        return found

    def catalogue(self, *, refresh: bool = False) -> list[AppInfo]:
        if self._cache is not None and not refresh:
            return self._cache

        merged: dict[str, AppInfo] = {}
        # Start Menu first: its names are the ones people actually see.
        for app in [*self._from_start_menu(), *self._from_registry()]:
            key = app.key.lower()
            if key not in merged:
                merged[key] = app

        self._cache = sorted(merged.values(), key=lambda a: a.name.lower())
        log.info("application catalogue built", count=len(self._cache))
        return self._cache

    def launch(self, app: AppInfo, *, arguments: list[str] | None = None) -> LaunchResult:
        """Start the program the way the shell would.

        `ShellExecuteExW` handles shortcuts, file associations and Store apps,
        which `CreateProcess` on a raw path does not.
        """
        import ctypes as c

        class SHELLEXECUTEINFOW(c.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("fMask", c.c_ulong),
                ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", c.c_int),
                ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", c.c_void_p),
                ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD),
                ("hIcon", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        info = SHELLEXECUTEINFOW()
        info.cbSize = c.sizeof(info)
        info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI
        info.lpVerb = "open"
        info.lpFile = app.launch_target
        info.lpParameters = " ".join(f'"{a}"' for a in (arguments or [])) or None
        info.nShow = SW_SHOWNORMAL

        shell32 = c.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = [c.POINTER(SHELLEXECUTEINFOW)]
        shell32.ShellExecuteExW.restype = wintypes.BOOL

        if not shell32.ShellExecuteExW(c.byref(info)):
            code = c.get_last_error()
            raise JarvisError(
                f"Windows could not start {app.name} (error {code}). "
                f"The program may have been moved or uninstalled.",
                app=app.name,
                error_code=code,
            )

        pid = 0
        if info.hProcess:
            kernel32 = c.WinDLL("kernel32", use_last_error=True)
            kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
            kernel32.GetProcessId.restype = wintypes.DWORD
            pid = int(kernel32.GetProcessId(info.hProcess))
            kernel32.CloseHandle(info.hProcess)

        return LaunchResult(app=app, pid=pid, detail=f"ShellExecuteExW on {app.launch_target}")


def available() -> bool:
    return sys.platform == "win32"


__all__ = [
    "PsutilProcessBackend",
    "Win32AppBackend",
    "Win32WindowBackend",
    "available",
]

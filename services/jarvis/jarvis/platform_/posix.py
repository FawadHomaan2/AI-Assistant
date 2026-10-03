"""POSIX adapters — Linux and macOS.

Jarvis targets Windows, so this backend exists for two reasons: development and
CI run here, and the process layer is genuinely cross-platform through psutil,
which means most of the logic above it is exercised for real rather than mocked.

Window management is the exception. There is no portable way to enumerate and
control another application's windows on Linux, and pretending otherwise by
returning an empty list would read as "you have no windows open". Those methods
raise `PlatformUnsupported` instead.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import psutil

from jarvis.platform_.base import AppBackend, ProcessBackend, WindowBackend
from jarvis.platform_.protected import check as check_protected
from jarvis.platform_.types import AppInfo, LaunchResult, ProcessInfo, WindowInfo
from jarvis.util.errors import JarvisError, PlatformUnsupported
from jarvis.util.logging import get_logger

log = get_logger(__name__)


class PsutilProcessBackend(ProcessBackend):
    """Process inspection and termination. Identical on Windows and POSIX."""

    _FIELDS = ("pid", "name", "exe", "username", "memory_info", "create_time", "status")

    def _convert(self, proc: psutil.Process) -> ProcessInfo | None:
        try:
            info = proc.as_dict(attrs=self._FIELDS)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        name = info.get("name") or ""
        exe = info.get("exe") or ""
        protected, reason = check_protected(proc.pid, name, exe)
        memory = info.get("memory_info")
        return ProcessInfo(
            pid=proc.pid,
            name=name,
            exe=exe,
            username=info.get("username") or "",
            # cpu_percent needs two samples to mean anything; the tool asks for
            # a measured value explicitly rather than reporting a misleading 0.
            cpu_percent=0.0,
            memory_bytes=int(memory.rss) if memory else 0,
            started_at=float(info.get("create_time") or 0.0),
            status=info.get("status") or "",
            protected=protected,
            protection_reason=reason,
        )

    def list_all(self, *, limit: int = 500) -> list[ProcessInfo]:
        out: list[ProcessInfo] = []
        for proc in psutil.process_iter():
            converted = self._convert(proc)
            if converted is not None:
                out.append(converted)
            if len(out) >= limit:
                break
        out.sort(key=lambda p: p.memory_bytes, reverse=True)
        return out

    def get(self, pid: int) -> ProcessInfo | None:
        try:
            return self._convert(psutil.Process(pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def find(self, name: str) -> list[ProcessInfo]:
        wanted = name.strip().lower().removesuffix(".exe")
        if not wanted:
            return []
        return [
            p for p in self.list_all(limit=2000) if wanted in p.name.lower().removesuffix(".exe")
        ]

    def terminate(self, pid: int, *, force: bool = False, timeout: float = 5.0) -> bool:
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True  # already gone is the outcome we wanted

        name = proc.name()
        protected, reason = check_protected(pid, name, proc.exe() if proc.is_running() else "")
        if protected:
            # Checked here as well as in the tool: this is the last line before
            # the syscall, and it is not reachable by any approval path.
            raise JarvisError(
                f"Jarvis will not end {name} (pid {pid}) because {reason}.",
                pid=pid,
                process=name,
            )

        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
            proc.wait(timeout=timeout)
            return True
        except psutil.TimeoutExpired:
            return False
        except psutil.NoSuchProcess:
            return True
        except psutil.AccessDenied as exc:
            raise JarvisError(
                f"Windows refused permission to end {name} (pid {pid}). It may be "
                f"running as another user or with higher privileges.",
                pid=pid,
            ) from exc


class PosixWindowBackend(WindowBackend):
    """Not implemented, and says so.

    Returning an empty list here would be worse than failing: "you have no
    windows open" is a confident wrong answer.
    """

    _MESSAGE = (
        "Window management is implemented with the Windows API and is not "
        "available on this platform. Jarvis is a Windows assistant; this "
        "backend exists so the rest of the core can be developed and tested "
        "elsewhere."
    )

    def _unsupported(self) -> PlatformUnsupported:
        return PlatformUnsupported(self._MESSAGE, platform=sys.platform)

    def list_all(self, *, visible_only: bool = True) -> list[WindowInfo]:
        raise self._unsupported()

    def foreground(self) -> WindowInfo | None:
        raise self._unsupported()

    def focus(self, handle: int) -> bool:
        raise self._unsupported()

    def minimise(self, handle: int) -> bool:
        raise self._unsupported()

    def maximise(self, handle: int) -> bool:
        raise self._unsupported()

    def restore(self, handle: int) -> bool:
        raise self._unsupported()

    def close(self, handle: int) -> bool:
        raise self._unsupported()


class PosixAppBackend(AppBackend):
    """Applications from freedesktop `.desktop` entries, plus PATH executables."""

    _DESKTOP_DIRS = (
        "/usr/share/applications",
        "/usr/local/share/applications",
        "~/.local/share/applications",
    )

    def __init__(self) -> None:
        self._cache: list[AppInfo] | None = None

    def describe(self) -> str:
        return "freedesktop .desktop entries (development backend)"

    def _parse_desktop(self, path: Path) -> AppInfo | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        name, command, no_display = "", "", False
        for line in text.splitlines():
            if line.startswith("Name=") and not name:
                name = line[5:].strip()
            elif line.startswith("Exec=") and not command:
                # Strip the freedesktop field codes (%u, %F, ...).
                command = " ".join(w for w in line[5:].split() if not w.startswith("%"))
            elif line.startswith(("NoDisplay=true", "Hidden=true")):
                no_display = True
        if not name or not command or no_display:
            return None
        return AppInfo(
            key=path.stem.lower(),
            name=name,
            launch_target=command,
            source="desktop-entry",
        )

    def catalogue(self, *, refresh: bool = False) -> list[AppInfo]:
        if self._cache is not None and not refresh:
            return self._cache

        found: dict[str, AppInfo] = {}
        for directory in self._DESKTOP_DIRS:
            folder = Path(directory).expanduser()
            if not folder.is_dir():
                continue
            for entry in sorted(folder.glob("*.desktop")):
                app = self._parse_desktop(entry)
                if app and app.key not in found:
                    found[app.key] = app

        # A few well-known executables, so the development backend can resolve
        # something on a container with no desktop entries at all.
        for executable in ("firefox", "google-chrome", "chromium", "code", "gedit", "xterm"):
            if executable in found:
                continue
            if resolved := shutil.which(executable):
                found[executable] = AppInfo(
                    key=executable,
                    name=executable.replace("-", " ").title(),
                    launch_target=resolved,
                    source="path",
                )

        self._cache = sorted(found.values(), key=lambda a: a.name.lower())
        return self._cache

    def launch(self, app: AppInfo, *, arguments: list[str] | None = None) -> LaunchResult:
        command = shlex.split(app.launch_target) + list(arguments or [])
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=os.environ.copy(),
            )
        except (OSError, ValueError) as exc:
            raise JarvisError(f"Could not start {app.name}: {exc}", app=app.name) from exc
        return LaunchResult(app=app, pid=proc.pid, detail=f"started {command[0]}")

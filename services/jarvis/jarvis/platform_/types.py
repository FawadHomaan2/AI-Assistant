"""Shared types for the OS adapters.

These shapes are platform-neutral on purpose: the tools above them are written
once, and only the backend below them differs between Windows and POSIX.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProcessInfo:
    pid: int
    name: str
    exe: str = ""
    username: str = ""
    cpu_percent: float = 0.0
    memory_bytes: int = 0
    started_at: float = 0.0
    status: str = ""
    #: True when Jarvis refuses to end it; see `protection_reason`.
    protected: bool = False
    protection_reason: str = ""
    #: Number of visible top-level windows, when the backend can tell.
    window_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "name": self.name,
            "exe": self.exe,
            "username": self.username,
            "cpuPercent": round(self.cpu_percent, 1),
            "memoryBytes": self.memory_bytes,
            "startedAt": self.started_at,
            "status": self.status,
            "protected": self.protected,
            "protectionReason": self.protection_reason,
            "windowCount": self.window_count,
        }


@dataclass
class WindowInfo:
    handle: int
    title: str
    pid: int
    process_name: str = ""
    visible: bool = True
    minimised: bool = False
    maximised: bool = False
    foreground: bool = False
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "title": self.title,
            "pid": self.pid,
            "processName": self.process_name,
            "visible": self.visible,
            "minimised": self.minimised,
            "maximised": self.maximised,
            "foreground": self.foreground,
            "bounds": list(self.bounds),
        }


@dataclass
class AppInfo:
    """An application Jarvis knows how to start.

    `launch_target` is what the backend actually executes. It is produced by the
    catalogue, never by the model — that distinction is the whole point: "open
    Chrome" resolves against installed software rather than letting a string
    become a command line.
    """

    key: str
    name: str
    launch_target: str
    source: str = ""
    icon: str = ""
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "source": self.source,
            "aliases": self.aliases,
        }


@dataclass
class LaunchResult:
    app: AppInfo
    pid: int = 0
    already_running: bool = False
    detail: str = ""

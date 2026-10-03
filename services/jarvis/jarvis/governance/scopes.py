"""Capability scopes — the second axis of the permission model (ARCHITECTURE §7).

A scope is a named capability the user grants, like an OAuth scope. Path scopes
are hierarchical: granting `fs.write` on Desktop covers its children but not its
siblings. Nothing is implicit; a tool declares what it needs and the policy
engine checks every one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class Scope(StrEnum):
    FS_READ = "fs.read"
    FS_WRITE = "fs.write"
    FS_DELETE = "fs.delete"
    APP_LAUNCH = "app.launch"
    APP_CONTROL = "app.control"
    WINDOW_MANAGE = "window.manage"
    PROCESS_READ = "process.read"
    PROCESS_KILL = "process.kill"
    SYSTEM_INFO = "system.info"
    SYSTEM_SETTINGS = "system.settings"
    SYSTEM_ADMIN = "system.admin"
    NET_READ = "net.read"
    NET_CONFIGURE = "net.configure"
    SHELL_RUN = "shell.run"
    SHELL_POWERSHELL = "shell.powershell"
    BROWSER_USE = "browser.use"
    BROWSER_DOWNLOAD = "browser.download"
    BROWSER_UPLOAD = "browser.upload"
    SCREEN_CAPTURE = "screen.capture"
    SCREEN_OCR = "screen.ocr"
    MIC_LISTEN = "mic.listen"
    SPEAKER_SPEAK = "speaker.speak"
    SECURITY_READ = "security.read"
    SECURITY_MODIFY = "security.modify"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    CLOUD_LLM = "cloud.llm"
    CLOUD_SEND = "cloud.send"


#: Granted on a fresh install. Deliberately narrow: reading and writing the
#: user's own document folders, launching apps, and reading system state.
#: Deleting, shell access, the browser, the microphone and the screen are off.
DEFAULT_GRANTS: frozenset[Scope] = frozenset(
    {
        Scope.FS_READ,
        Scope.FS_WRITE,
        Scope.APP_LAUNCH,
        Scope.PROCESS_READ,
        Scope.SYSTEM_INFO,
        Scope.SECURITY_READ,
    }
)

#: Human wording for the consent prompt and the privacy dashboard.
DESCRIPTIONS: dict[Scope, str] = {
    Scope.FS_READ: "Read files in allowed folders",
    Scope.FS_WRITE: "Create and change files in allowed folders",
    Scope.FS_DELETE: "Delete files (to the Recycle Bin unless you say otherwise)",
    Scope.APP_LAUNCH: "Start applications",
    Scope.APP_CONTROL: "Focus, minimise and close windows",
    Scope.WINDOW_MANAGE: "Move and resize windows",
    Scope.PROCESS_READ: "List running programs",
    Scope.PROCESS_KILL: "End running programs",
    Scope.SYSTEM_INFO: "Read CPU, memory, disk and network counters",
    Scope.SYSTEM_SETTINGS: "Change Windows settings",
    Scope.SYSTEM_ADMIN: "Perform administrator actions",
    Scope.NET_READ: "Read network configuration",
    Scope.NET_CONFIGURE: "Change network configuration",
    Scope.SHELL_RUN: "Run allowlisted commands",
    Scope.SHELL_POWERSHELL: "Run PowerShell",
    Scope.BROWSER_USE: "Drive a web browser",
    Scope.BROWSER_DOWNLOAD: "Download files",
    Scope.BROWSER_UPLOAD: "Upload files",
    Scope.SCREEN_CAPTURE: "Take screenshots",
    Scope.SCREEN_OCR: "Read text from the screen",
    Scope.MIC_LISTEN: "Use the microphone",
    Scope.SPEAKER_SPEAK: "Speak through the speakers",
    Scope.SECURITY_READ: "Read security state",
    Scope.SECURITY_MODIFY: "Change security settings",
    Scope.MEMORY_READ: "Read remembered preferences",
    Scope.MEMORY_WRITE: "Remember preferences",
    Scope.CLOUD_LLM: "Send prompts to a cloud AI provider",
    Scope.CLOUD_SEND: "Send your data to an external service",
}


@dataclass
class ScopeGrants:
    """What the user has allowed. Revocable at any time."""

    granted: set[Scope] = field(default_factory=lambda: set(DEFAULT_GRANTS))
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def has(self, scope: Scope) -> bool:
        return scope in self.granted

    def missing(self, required: list[Scope]) -> list[Scope]:
        return [s for s in required if s not in self.granted]

    def grant(self, scope: Scope) -> None:
        self.granted.add(scope)
        self._touch()

    def revoke(self, scope: Scope) -> None:
        self.granted.discard(scope)
        self._touch()

    def _touch(self) -> None:
        self.updated_at = datetime.now(UTC).isoformat(timespec="seconds")

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                "scope": scope.value,
                "description": DESCRIPTIONS.get(scope, scope.value),
                "granted": scope in self.granted,
            }
            for scope in Scope
        ]

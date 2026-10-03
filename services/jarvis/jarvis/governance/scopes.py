"""Capability scopes — the second axis of the permission model (ARCHITECTURE §7).

A scope is a named capability the user grants, like an OAuth scope. Nothing is
implicit: a tool declares what it needs and the policy engine checks every one.

**Path scopes are hierarchical and targeted.** `fs.write` on Desktop covers
`Desktop/notes/todo.txt` and does not cover Documents. That distinction is the
whole point — a single global "can write files" grant would make the folder
permissions decorative, since the path jail and the grant would then always
agree. Comparison is done on normalised paths, and a grant with no target means
"wherever this scope otherwise applies", which for filesystem scopes still
leaves the path jail in the way.

**Grants expire and are revocable.** A grant made for one task should not
outlive it, so `expires_at` is honoured on every check rather than swept
periodically — a lazily-expired grant that is never swept is a grant that still
works.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any


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


#: Scopes whose grants can name a folder. For these, a grant without a target
#: is broad, and the interface says so rather than showing a bare tick.
PATH_SCOPES: frozenset[Scope] = frozenset({Scope.FS_READ, Scope.FS_WRITE, Scope.FS_DELETE})

#: Scopes that can never be granted permanently in advance. Each use is
#: confirmed in the moment, because the thing being authorised — sending your
#: data somewhere, running arbitrary shell — depends entirely on what is being
#: sent or run, which a standing grant cannot describe.
NEVER_BLANKET: frozenset[Scope] = frozenset(
    {Scope.SYSTEM_ADMIN, Scope.SHELL_POWERSHELL, Scope.CLOUD_SEND, Scope.SECURITY_MODIFY}
)

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

#: What goes wrong if this is granted, in one sentence. Shown next to the
#: toggle: a permission screen that lists capabilities without consequences is
#: one people click through.
CONSEQUENCES: dict[Scope, str] = {
    Scope.FS_DELETE: "Jarvis can move your files to the Recycle Bin without asking each time.",
    Scope.PROCESS_KILL: "Jarvis can end running programs, losing unsaved work in them.",
    Scope.SYSTEM_SETTINGS: "Jarvis can change how Windows is configured.",
    Scope.SYSTEM_ADMIN: "Jarvis can act as an administrator. Confirmed every time regardless.",
    Scope.SHELL_RUN: "Jarvis can run commands from a fixed allowlist.",
    Scope.SHELL_POWERSHELL: "Jarvis can run PowerShell. Confirmed every time regardless.",
    Scope.BROWSER_USE: "Jarvis can visit allowlisted websites in its own browser profile.",
    Scope.BROWSER_UPLOAD: "Jarvis can send your files to a website.",
    Scope.SCREEN_CAPTURE: "Jarvis can see whatever is on your screen, including other apps.",
    Scope.MIC_LISTEN: "Jarvis can hear the room when listening is on.",
    Scope.SECURITY_MODIFY: "Not implemented: Jarvis never changes security settings.",
    Scope.CLOUD_LLM: "Your messages leave this computer and go to a cloud AI provider.",
    Scope.CLOUD_SEND: "Your data leaves this computer. Confirmed every time regardless.",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _default_grants() -> list[Grant]:
    return [Grant(scope=s, source="default") for s in sorted(DEFAULT_GRANTS)]


def normalise(path: str) -> str:
    """Compare paths the way the filesystem does, without touching the disk.

    `resolve()` is not used on purpose: a grant may name a folder that does not
    exist yet, and resolving would turn that into the current directory.
    """
    if not path:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    return os.path.normcase(os.path.normpath(expanded)).rstrip("\\/") or os.sep


def covers(grant_target: str, requested: str) -> bool:
    """Whether a grant on one folder covers a path.

    Hierarchical, and careful about the sibling case: a grant on `/home/me/doc`
    must not cover `/home/me/documents`, which a naive `startswith` would allow.
    """
    if not grant_target:
        return True  # an untargeted grant applies wherever the scope does
    if not requested:
        return False  # a targeted grant never covers "unspecified"
    base = normalise(grant_target)
    target = normalise(requested)
    if base == target:
        return True
    return target.startswith(base + os.sep) or target.startswith(base + "/")


@dataclass(frozen=True)
class Grant:
    """One capability, optionally limited to a folder and to a time."""

    scope: Scope
    #: Empty means "wherever this scope applies".
    target: str = ""
    #: ISO timestamp. Empty means it does not expire.
    expires_at: str = ""
    #: 'default' on install, 'user' when granted deliberately, 'session' when
    #: granted for one conversation.
    source: str = "user"
    granted_at: str = field(default_factory=lambda: _now().isoformat(timespec="seconds"))

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            return datetime.fromisoformat(self.expires_at) <= _now()
        except ValueError:
            # An unparseable expiry is treated as expired. Failing closed is the
            # only safe reading of "I cannot tell when this should stop".
            return True

    def permits(self, scope: Scope, target: str = "") -> bool:
        if self.scope is not scope or self.expired:
            return False
        if scope not in PATH_SCOPES:
            return True
        return covers(self.target, target)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "target": self.target,
            "expiresAt": self.expires_at,
            "source": self.source,
            "grantedAt": self.granted_at,
            "expired": self.expired,
        }


@dataclass
class ScopeGrants:
    """What the user has allowed. Revocable at any time."""

    #: Defaults to the install grant set. Passing `[]` means exactly that —
    #: nothing granted — rather than springing back to the defaults, which
    #: would be the opposite of revoking everything.
    grants: list[Grant] = field(default_factory=_default_grants)
    updated_at: str = field(default_factory=lambda: _now().isoformat(timespec="seconds"))

    @classmethod
    def of(cls, scopes: set[Scope] | frozenset[Scope]) -> ScopeGrants:
        """Build from a bare set. Used by tests and by the default install."""
        return cls(grants=[Grant(scope=s) for s in sorted(scopes)])

    @classmethod
    def empty(cls) -> ScopeGrants:
        """Nothing granted at all."""
        return cls(grants=[])

    # ── checking ─────────────────────────────────────────────────────────
    @property
    def live(self) -> list[Grant]:
        """Grants that exist and have not expired."""
        return [g for g in self.grants if not g.expired]

    def has(self, scope: Scope, target: str = "") -> bool:
        return any(g.permits(scope, target) for g in self.grants)

    def missing(self, required: list[Scope], targets: list[str] | None = None) -> list[Scope]:
        """Scopes that are not granted for these targets.

        A path scope is checked against every target: granting Desktop and then
        asking to write to Desktop *and* Documents is not permitted, and
        reporting it as permitted because one of them matched would be the kind
        of partial check that makes a permission system worthless.
        """
        paths = [t for t in (targets or []) if t]
        out: list[Scope] = []
        for scope in required:
            if scope in PATH_SCOPES and paths:
                if not all(self.has(scope, path) for path in paths):
                    out.append(scope)
            elif not self.has(scope):
                out.append(scope)
        return out

    @property
    def granted(self) -> set[Scope]:
        """The scopes with at least one live grant. For display and tests."""
        return {g.scope for g in self.live}

    # ── changing ─────────────────────────────────────────────────────────
    def grant(
        self,
        scope: Scope,
        target: str = "",
        *,
        source: str = "user",
        ttl_minutes: int | None = None,
    ) -> Grant:
        expires = ""
        if ttl_minutes:
            expires = (_now() + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
        new = Grant(scope=scope, target=target, expires_at=expires, source=source)
        # Replace an identical grant rather than stacking duplicates, so
        # revoking once actually revokes.
        self.grants = [g for g in self.grants if not (g.scope is scope and g.target == target)]
        self.grants.append(new)
        self._touch()
        return new

    def revoke(self, scope: Scope, target: str = "") -> int:
        """Remove grants. With no target, removes every grant for the scope."""
        before = len(self.grants)
        if target:
            self.grants = [g for g in self.grants if not (g.scope is scope and g.target == target)]
        else:
            self.grants = [g for g in self.grants if g.scope is not scope]
        self._touch()
        return before - len(self.grants)

    def purge_expired(self) -> int:
        before = len(self.grants)
        self.grants = self.live
        if before != len(self.grants):
            self._touch()
        return before - len(self.grants)

    def _touch(self) -> None:
        self.updated_at = _now().isoformat(timespec="seconds")

    # ── display ──────────────────────────────────────────────────────────
    def describe(self) -> list[dict[str, Any]]:
        live_scopes = {g.scope for g in self.live}
        return [
            {
                "scope": scope.value,
                "description": DESCRIPTIONS.get(scope, scope.value),
                "consequence": CONSEQUENCES.get(scope, ""),
                "granted": scope in live_scopes,
                "pathScoped": scope in PATH_SCOPES,
                "alwaysConfirmed": scope in NEVER_BLANKET,
                "targets": [g.to_dict() for g in self.grants if g.scope is scope and not g.expired],
            }
            for scope in Scope
        ]

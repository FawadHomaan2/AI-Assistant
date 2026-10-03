"""Processes Jarvis refuses to end.

Ending the wrong process is one of the few things an assistant can do that
leaves a machine unbootable or unprotected. This list is checked in the backend,
below the policy engine, so no mode, scope grant or confirmation can override
it — "the user approved it" is not a good enough reason to terminate the Windows
kernel session manager or the antivirus.

Three categories:
  1. **System critical** — ending these bluescreens Windows or breaks the shell.
  2. **Security** — ending these disables the machine's protection, which is
     exactly what malware does. Refused regardless of intent.
  3. **Jarvis itself** — terminating our own process mid-action is incoherent.
"""

from __future__ import annotations

import contextlib
import os
import sys

#: Ending any of these takes the machine down or logs the user out.
_WINDOWS_CRITICAL = frozenset(
    {
        "system",
        "system idle process",
        "registry",
        "memory compression",
        "smss.exe",  # session manager
        "csrss.exe",  # client/server runtime — terminating bluescreens
        "wininit.exe",  # Windows start-up
        "winlogon.exe",  # logon — terminating logs the user out
        "services.exe",  # service control manager
        "lsass.exe",  # local security authority — bluescreens
        "lsaiso.exe",  # credential guard
        "svchost.exe",  # hosts dozens of services; never safe to blanket-kill
        "fontdrvhost.exe",
        "dwm.exe",
        "sihost.exe",
        "ctfmon.exe",
        "explorer.exe",  # the desktop shell; recoverable but very disruptive
        "audiodg.exe",
        "conhost.exe",
        "runtimebroker.exe",
        "taskhostw.exe",
        "wudfhost.exe",
        "dllhost.exe",
        "spoolsv.exe",
    }
)

#: Security software. Disabling protection is never a routine request.
_SECURITY = frozenset(
    {
        "msmpeng.exe",  # Microsoft Defender antimalware service
        "mpdefendercoreservice.exe",
        "nissrv.exe",  # Defender network inspection
        "securityhealthservice.exe",
        "securityhealthsystray.exe",
        "mssense.exe",  # Defender for Endpoint
        "sensecncproxy.exe",
        "senseir.exe",
        "wscsvc",
        "windefend",
        # Common third-party agents, so "close my antivirus" is refused rather
        # than half-working.
        "avp.exe",
        "avgui.exe",
        "avastui.exe",
        "mbam.exe",
        "mbamservice.exe",
        "ekrn.exe",
        "egui.exe",
        "ccsvchst.exe",
        "nortonsecurity.exe",
        "bdagent.exe",
        "vsserv.exe",
        "cbdaemon",
        "csfalconservice.exe",
        "sentinelagent.exe",
        "sophosui.exe",
        "savservice.exe",
    }
)

_POSIX_CRITICAL = frozenset(
    {"systemd", "init", "kthreadd", "kernel_task", "launchd", "dbus-daemon", "sshd"}
)

#: Our own process names, so Jarvis cannot terminate itself mid-action.
_SELF = frozenset({"jarvis.exe", "jarvis-core.exe", "jarvis-core"})


def _own_pids() -> set[int]:
    pids = {os.getpid()}
    with contextlib.suppress(OSError):
        pids.add(os.getppid())
    return pids


def check(pid: int, name: str, exe: str = "") -> tuple[bool, str]:
    """Is this process protected, and why?

    Returns (protected, reason). The reason is shown to the user, so it explains
    the consequence rather than just saying "no".
    """
    lowered = name.strip().lower()

    if pid in _own_pids():
        return True, "this is Jarvis itself"
    if lowered in _SELF:
        return True, "this is part of Jarvis"
    # PID 0 and 4 are the idle process and the kernel on Windows.
    if pid in (0, 4):
        return True, "this is a kernel process"
    if pid == 1 and sys.platform != "win32":
        return True, "this is the system init process"

    if lowered in _SECURITY:
        return True, (
            "it is security software — ending it would leave this computer "
            "unprotected, which Jarvis will not do"
        )

    # Both lists are checked on every platform, for two reasons: a process named
    # `csrss.exe` on Linux is not legitimate anyway, and gating the Windows list
    # behind a platform check would make it untestable anywhere but Windows —
    # which is exactly the wrong property for a security-critical list.
    if lowered in _WINDOWS_CRITICAL:
        return True, (
            "it is a core Windows process — ending it would crash this computer or log you out"
        )
    if lowered in _POSIX_CRITICAL:
        return True, "it is a core system process"

    # A process running from a system directory is system software even if its
    # name is not on the list.
    if exe:
        lowered_exe = exe.replace("/", "\\").lower()
        for marker in ("\\windows\\system32\\", "\\windows\\syswow64\\", "\\windows\\servicing\\"):
            if marker in lowered_exe:
                return True, "it runs from a protected Windows system directory"

    return False, ""


def is_protected(pid: int, name: str, exe: str = "") -> bool:
    return check(pid, name, exe)[0]

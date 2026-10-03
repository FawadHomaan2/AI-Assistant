"""Reading this machine's security posture.

Split from the checks on purpose: collectors return *measurements*, checks turn
measurements into claims. That boundary is what lets the judgement — which is
where a security tool goes wrong — be tested on a machine that has no Defender
and no BitLocker.

Every measurement is a `Reading`, which has three states rather than two:
present, absent, and **unknown**. The third is the important one. A machine
where Jarvis could not read the firewall state must not report "firewall off",
and must not quietly report nothing either: it reports that it could not look,
and says what it would take.

On Windows these call PowerShell cmdlets through the existing read-only
allowlist. Those paths cannot be executed in this project's Linux CI and are
marked as such in docs/PHASES.md — the POSIX equivalents below are real, and
are what the tests exercise.
"""

from __future__ import annotations

import abc
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: A posture command that hangs would hang the whole scan.
COMMAND_TIMEOUT = 15.0

#: Addresses that accept connections from anywhere rather than only from this
#: machine. Jarvis matches against these; it never binds to them.
ANY_ADDRESS = ("0.0.0.0", "::", "*", "")  # noqa: S104

#: Addresses only this machine can reach.
LOOPBACK_ADDRESS = ("::1", "localhost")


def is_external(address: str) -> bool:
    """Whether something listening here is reachable from off this machine."""
    cleaned = (address or "").strip().strip("[]").lower()
    if cleaned in ANY_ADDRESS:
        return True
    return not (cleaned.startswith("127.") or cleaned in LOOPBACK_ADDRESS)


def _platform() -> str:
    """Read through a function so mypy does not narrow other branches away."""
    return platform.system().lower()


def on_windows() -> bool:
    return _platform() == "windows"


@dataclass
class Reading:
    """One measurement, or an honest statement that it could not be taken."""

    #: What was measured, when it could be.
    value: Any = None
    #: False when the measurement could not be taken at all.
    known: bool = False
    #: Why not, and what it would take. Only set when `known` is False.
    reason: str = ""
    #: The command or file the value came from, so a claim is checkable.
    source: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def unknown(cls, reason: str, source: str = "") -> Reading:
        return cls(value=None, known=False, reason=reason, source=source)

    @classmethod
    def measured(cls, value: Any, source: str, **detail: Any) -> Reading:
        return cls(value=value, known=True, source=source, detail=detail)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "known": self.known,
            "reason": self.reason,
            "source": self.source,
            "detail": self.detail,
        }


@dataclass
class StartupItem:
    name: str
    command: str
    location: str
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "location": self.location,
            "enabled": self.enabled,
        }


@dataclass
class Listener:
    """A program accepting connections, and from where."""

    port: int
    address: str
    protocol: str
    pid: int | None
    process: str
    exe: str = ""
    #: True when it is reachable from outside this machine.
    external: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "address": self.address,
            "protocol": self.protocol,
            "pid": self.pid,
            "process": self.process,
            "exe": self.exe,
            "external": self.external,
        }


def _run(command: list[str]) -> tuple[bool, str]:
    """Run a read-only command. Returns (ok, output-or-reason)."""
    if not shutil.which(command[0]):
        return False, f"{command[0]} is not installed on this machine."
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, never a shell string
            command,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"{command[0]} did not answer within {COMMAND_TIMEOUT:.0f} seconds."
    except OSError as exc:
        return False, f"Could not run {command[0]}: {exc}"
    if completed.returncode != 0:
        reason = (completed.stderr or completed.stdout).strip().splitlines()
        return False, (reason[0] if reason else f"{command[0]} exited {completed.returncode}.")
    return True, completed.stdout


class PostureCollector(abc.ABC):
    """What a platform can tell us about its own defences."""

    name = "unknown"

    @abc.abstractmethod
    def antivirus(self) -> Reading: ...

    @abc.abstractmethod
    def firewall(self) -> Reading: ...

    @abc.abstractmethod
    def disk_encryption(self) -> Reading: ...

    @abc.abstractmethod
    def updates(self) -> Reading: ...

    @abc.abstractmethod
    def startup_items(self) -> Reading: ...

    @abc.abstractmethod
    def removable_devices(self) -> Reading: ...

    def listeners(self) -> Reading:
        """Programs accepting network connections.

        psutil behaves the same on Windows and POSIX, so this is implemented
        once and is genuinely verified rather than written blind. It needs
        elevation to name the owning process of another user's socket; when it
        cannot, the port is still reported and the process is left empty rather
        than guessed.
        """
        try:
            connections = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, PermissionError):
            return Reading.unknown(
                "Listing network connections needs administrator rights on this "
                "machine. Run Jarvis as administrator to include them.",
                source="psutil.net_connections",
            )
        except (OSError, RuntimeError) as exc:
            return Reading.unknown(f"Could not read network connections: {exc}")

        listeners: list[Listener] = []
        for conn in connections:
            if conn.status != psutil.CONN_LISTEN or conn.laddr is None:
                continue
            address = conn.laddr.ip
            name, exe = "", ""
            if conn.pid:
                try:
                    proc = psutil.Process(conn.pid)
                    name = proc.name()
                    exe = proc.exe()
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    name = ""
            listeners.append(
                Listener(
                    port=conn.laddr.port,
                    address=address,
                    protocol="tcp" if conn.type == 1 else "udp",
                    pid=conn.pid,
                    process=name,
                    exe=exe,
                    external=is_external(address),
                )
            )
        return Reading.measured(
            [listener.to_dict() for listener in listeners],
            source="psutil.net_connections",
            count=len(listeners),
        )


class WindowsCollector(PostureCollector):
    """Windows posture, read through read-only PowerShell cmdlets.

    Every cmdlet here is a `Get-`: this collector observes and never changes
    anything. Changing Defender or the firewall needs administrator rights and
    is deliberately not implemented — Jarvis reports, and the user decides.
    """

    name = "windows"

    @staticmethod
    def _powershell(script: str) -> tuple[bool, str]:
        return _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ]
        )

    def _json(self, script: str, source: str) -> Reading:
        ok, output = self._powershell(f"{script} | ConvertTo-Json -Compress -Depth 4")
        if not ok:
            return Reading.unknown(output, source=source)
        text = output.strip()
        if not text:
            return Reading.unknown(f"{source} returned nothing.", source=source)
        try:
            return Reading.measured(json.loads(text), source=source)
        except json.JSONDecodeError:
            return Reading.unknown(
                f"{source} returned output Jarvis could not read.", source=source
            )

    def antivirus(self) -> Reading:
        return self._json(
            "Get-MpComputerStatus | Select-Object AMServiceEnabled,RealTimeProtectionEnabled,"
            "AntivirusEnabled,AntispywareEnabled,AntivirusSignatureAge,"
            "AntivirusSignatureLastUpdated,QuickScanAge,FullScanAge",
            "Get-MpComputerStatus",
        )

    def firewall(self) -> Reading:
        return self._json(
            "Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction",
            "Get-NetFirewallProfile",
        )

    def disk_encryption(self) -> Reading:
        return self._json(
            "Get-BitLockerVolume | Select-Object MountPoint,VolumeStatus,"
            "ProtectionStatus,EncryptionPercentage",
            "Get-BitLockerVolume",
        )

    def updates(self) -> Reading:
        return self._json(
            "Get-HotFix | Sort-Object InstalledOn -Descending | "
            "Select-Object -First 5 HotFixID,InstalledOn,Description",
            "Get-HotFix",
        )

    def startup_items(self) -> Reading:
        return self._json(
            "Get-CimInstance Win32_StartupCommand | Select-Object Name,Command,Location,User",
            "Win32_StartupCommand",
        )

    def removable_devices(self) -> Reading:
        return self._json(
            "Get-CimInstance Win32_DiskDrive | Where-Object { $_.InterfaceType -eq 'USB' } | "
            "Select-Object Model,SerialNumber,Size",
            "Win32_DiskDrive",
        )


class PosixCollector(PostureCollector):
    """The POSIX equivalents, which are real and are what CI exercises.

    Several have no honest equivalent — there is no "Windows Update" on Linux
    in the sense the Security Center means — and those say so rather than
    inventing a status.
    """

    name = "posix"

    def antivirus(self) -> Reading:
        # ClamAV is the only common on-access scanner, and most Linux desktops
        # run none at all. "No antivirus" is normal here, not a finding.
        if shutil.which("clamscan") or shutil.which("freshclam"):
            ok, output = _run(["clamscan", "--version"])
            if ok:
                return Reading.measured(
                    {"product": output.strip(), "realtime": False},
                    source="clamscan --version",
                )
        return Reading.unknown(
            "This is not Windows, so there is no Defender to read. Antivirus "
            "status is a Windows check.",
            source="platform",
        )

    def firewall(self) -> Reading:
        if shutil.which("ufw"):
            ok, output = _run(["ufw", "status"])
            if ok:
                enabled = "status: active" in output.lower()
                return Reading.measured({"product": "ufw", "enabled": enabled}, source="ufw status")
            return Reading.unknown(
                "Reading the firewall state needs root on this machine.", source="ufw status"
            )
        if shutil.which("nft"):
            ok, output = _run(["nft", "list", "ruleset"])
            if ok:
                return Reading.measured(
                    {"product": "nftables", "enabled": bool(output.strip())},
                    source="nft list ruleset",
                )
            return Reading.unknown(
                "Reading the nftables ruleset needs root on this machine.",
                source="nft list ruleset",
            )
        return Reading.unknown(
            "No firewall tool Jarvis recognises (ufw, nftables) is installed.",
            source="platform",
        )

    def disk_encryption(self) -> Reading:
        if not shutil.which("lsblk"):
            return Reading.unknown("lsblk is not installed, so disk encryption cannot be read.")
        ok, output = _run(["lsblk", "-o", "NAME,FSTYPE,MOUNTPOINT", "-J"])
        if not ok:
            return Reading.unknown(output, source="lsblk")
        try:
            tree = json.loads(output)
        except json.JSONDecodeError:
            return Reading.unknown("lsblk returned output Jarvis could not read.", source="lsblk")

        encrypted: list[str] = []
        plain: list[str] = []

        def walk(nodes: list[dict[str, Any]]) -> None:
            for node in nodes:
                fstype = (node.get("fstype") or "").lower()
                name = str(node.get("name", ""))
                if fstype == "crypto_luks":
                    encrypted.append(name)
                elif node.get("mountpoint") and fstype:
                    plain.append(name)
                walk(node.get("children", []) or [])

        walk(tree.get("blockdevices", []))
        return Reading.measured(
            {"encrypted": encrypted, "unencrypted": plain},
            source="lsblk -J",
        )

    def updates(self) -> Reading:
        return Reading.unknown(
            "Checking for pending updates is a Windows Update check and has no equivalent here.",
            source="platform",
        )

    def startup_items(self) -> Reading:
        """XDG autostart entries and enabled systemd user units.

        The real POSIX equivalent of the Run keys, and genuinely readable, so
        the startup check is exercised rather than written blind.
        """
        items: list[StartupItem] = []
        home = Path(os.path.expanduser("~"))
        for directory in (home / ".config/autostart", Path("/etc/xdg/autostart")):
            if not directory.is_dir():
                continue
            for entry in sorted(directory.glob("*.desktop")):
                try:
                    text = entry.read_text("utf-8", errors="replace")
                except OSError:
                    continue
                name = _desktop_field(text, "Name") or entry.stem
                command = _desktop_field(text, "Exec")
                hidden = _desktop_field(text, "Hidden").lower() == "true"
                items.append(
                    StartupItem(
                        name=name,
                        command=command,
                        location=str(directory),
                        enabled=not hidden,
                    )
                )

        if shutil.which("systemctl"):
            ok, output = _run(
                ["systemctl", "--user", "list-unit-files", "--state=enabled", "--no-legend"]
            )
            if ok:
                for line in output.splitlines():
                    parts = line.split()
                    if parts:
                        items.append(
                            StartupItem(
                                name=parts[0],
                                command=parts[0],
                                location="systemd --user",
                                enabled=True,
                            )
                        )

        return Reading.measured(
            [i.to_dict() for i in items], source="XDG autostart + systemd --user"
        )

    def removable_devices(self) -> Reading:
        if not shutil.which("lsblk"):
            return Reading.unknown("lsblk is not installed, so devices cannot be listed.")
        ok, output = _run(["lsblk", "-o", "NAME,RM,SIZE,MODEL,SERIAL", "-J"])
        if not ok:
            return Reading.unknown(output, source="lsblk")
        try:
            tree = json.loads(output)
        except json.JSONDecodeError:
            return Reading.unknown("lsblk returned output Jarvis could not read.", source="lsblk")
        removable = [
            {
                "model": (node.get("model") or "").strip(),
                "serialNumber": (node.get("serial") or "").strip(),
                "size": node.get("size"),
                "name": node.get("name"),
            }
            for node in tree.get("blockdevices", [])
            if node.get("rm")
        ]
        return Reading.measured(removable, source="lsblk -J")


_DESKTOP_FIELD = r"^{0}\s*=\s*(?P<v>.*)$"


def _desktop_field(text: str, field_name: str) -> str:
    match = re.search(_DESKTOP_FIELD.format(re.escape(field_name)), text, re.MULTILINE)
    return match.group("v").strip() if match else ""


def collector() -> PostureCollector:
    return WindowsCollector() if on_windows() else PosixCollector()

"""The checks: measurements in, claims out.

Each check is small and does one thing, because the alternative — one function
that reads everything and decides everything — is where a security tool quietly
starts asserting more than it measured.

Three rules hold for every check here.

**A check that could not run says so.** It never returns an empty list, because
"no findings" reads as "all clear", which is the most dangerous thing a security
tool can say when it has not actually looked.

**Novelty is reported, never accused.** A startup entry Jarvis has not seen
before produces a `NORMAL` finding that says it is new. Only a named risky
pattern produces `SUSPICIOUS`, and the pattern's name goes into the evidence.

**Every claim carries its evidence.** The `evidence` dict holds what was
measured and where it came from, so the user can disagree with the conclusion
and still trust the facts.
"""

from __future__ import annotations

import abc
import time
from typing import Any

from jarvis.security.baseline import Baseline
from jarvis.security.classify import Signals, classify, novelty_note, severity_for
from jarvis.security.collectors import PostureCollector, Reading
from jarvis.security.types import (
    Category,
    CheckResult,
    Classification,
    Finding,
    Severity,
    fingerprint,
)
from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: Ports that are a normal part of a desktop and not worth alarming about.
COMMON_LOCAL_PORTS = frozenset({135, 139, 445, 5353, 1900, 5355, 631, 68, 546})

#: Directories a long-running network listener has no business living in. This
#: is the named pattern behind a SUSPICIOUS network finding — not "I do not
#: recognise this program", which is not evidence of anything.
VOLATILE_PATHS = (
    "\\appdata\\local\\temp",
    "\\windows\\temp",
    "/tmp/",  # noqa: S108 — matched against another program's path, never used
    "/var/tmp/",  # noqa: S108
    "/dev/shm/",  # noqa: S108
    "\\downloads\\",
    "/downloads/",
)


class SecurityCheck(abc.ABC):
    category: Category
    name: str

    def __init__(self, collector: PostureCollector, baseline: Baseline) -> None:
        self.collector = collector
        self.baseline = baseline

    @abc.abstractmethod
    def run(self) -> CheckResult: ...

    def unavailable(self, reading: Reading) -> CheckResult:
        """The honest result when a measurement could not be taken."""
        return CheckResult(
            category=self.category,
            name=self.name,
            ran=False,
            unavailable_reason=reading.reason,
            summary=f"{self.name} could not be checked.",
        )

    def finding(
        self,
        title: str,
        *,
        explanation: str,
        evidence: dict[str, Any],
        signals: Signals,
        base_severity: Severity,
        remediation: str = "",
        print_fingerprint: str = "",
    ) -> Finding:
        """Build a finding with its classification capped to what was measured."""
        classification = classify(signals)
        return Finding(
            category=self.category,
            title=title,
            classification=classification,
            severity=severity_for(classification, base=base_severity),
            explanation=explanation,
            evidence={**evidence, "matchedPattern": signals.matched_pattern or None},
            remediation=remediation,
            novel=signals.novel,
            fingerprint=print_fingerprint or fingerprint(self.category.value, title),
        )


def _timed(fn: Any) -> Any:
    """Record how long a check took, so a slow one is visible rather than felt."""

    def wrapper(self: SecurityCheck) -> CheckResult:
        started = time.monotonic()
        result: CheckResult = fn(self)
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    return wrapper


class AntivirusCheck(SecurityCheck):
    category = Category.ANTIVIRUS
    name = "Antivirus"

    @_timed
    def run(self) -> CheckResult:
        reading = self.collector.antivirus()
        if not reading.known:
            return self.unavailable(reading)

        data = reading.value if isinstance(reading.value, dict) else {}
        realtime = data.get("RealTimeProtectionEnabled", data.get("realtime"))
        findings: list[Finding] = []

        if realtime is False:
            findings.append(
                self.finding(
                    "Real-time protection is turned off",
                    explanation=(
                        "Antivirus is installed but is not watching files as they "
                        "are opened. This is a measured setting, not a guess."
                    ),
                    evidence={"realTimeProtection": False, "source": reading.source},
                    signals=Signals(weakness=True, observed_fact=True),
                    base_severity=Severity.HIGH,
                    remediation=(
                        "Turn real-time protection back on in Windows Security. "
                        "Jarvis does not change this setting itself."
                    ),
                )
            )

        age = data.get("AntivirusSignatureAge")
        if isinstance(age, int | float) and age > 7:
            findings.append(
                self.finding(
                    f"Virus definitions are {int(age)} days old",
                    explanation=(
                        "Definitions this old will not recognise anything "
                        "discovered since they were published."
                    ),
                    evidence={"signatureAgeDays": age, "source": reading.source},
                    signals=Signals(weakness=True),
                    base_severity=Severity.MEDIUM,
                    remediation="Check for updates in Windows Security.",
                )
            )

        if not findings:
            findings.append(
                self.finding(
                    "Antivirus is running and up to date",
                    explanation="Real-time protection is on and definitions are current.",
                    evidence={k: v for k, v in data.items() if v is not None},
                    signals=Signals(),
                    base_severity=Severity.INFO,
                )
            )

        return CheckResult(
            category=self.category,
            name=self.name,
            ran=True,
            findings=findings,
            summary=f"Read antivirus status from {reading.source}.",
        )


class FirewallCheck(SecurityCheck):
    category = Category.FIREWALL
    name = "Firewall"

    @_timed
    def run(self) -> CheckResult:
        reading = self.collector.firewall()
        if not reading.known:
            return self.unavailable(reading)

        profiles = reading.value if isinstance(reading.value, list) else [reading.value]
        findings: list[Finding] = []
        off: list[str] = []
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            label = str(profile.get("Name") or profile.get("product") or "firewall")
            enabled = profile.get("Enabled", profile.get("enabled"))
            # Windows reports Enabled as 1/0 or True/False depending on version.
            if enabled in (False, 0, "False"):
                off.append(label)

        if off:
            findings.append(
                self.finding(
                    f"Firewall is off for: {', '.join(off)}",
                    explanation=(
                        "With the firewall off, anything listening on this machine "
                        "can be reached from the network it is attached to."
                    ),
                    evidence={"disabledProfiles": off, "source": reading.source},
                    signals=Signals(weakness=True, observed_fact=True),
                    base_severity=Severity.HIGH,
                    remediation="Turn the firewall back on for those profiles.",
                )
            )
        else:
            findings.append(
                self.finding(
                    "Firewall is on",
                    explanation="Inbound connections are filtered.",
                    evidence={"profiles": profiles, "source": reading.source},
                    signals=Signals(),
                    base_severity=Severity.INFO,
                )
            )

        return CheckResult(
            category=self.category,
            name=self.name,
            ran=True,
            findings=findings,
            summary=f"Read firewall state from {reading.source}.",
        )


class EncryptionCheck(SecurityCheck):
    category = Category.ENCRYPTION
    name = "Disk encryption"

    @_timed
    def run(self) -> CheckResult:
        reading = self.collector.disk_encryption()
        if not reading.known:
            return self.unavailable(reading)

        value = reading.value
        unprotected: list[str] = []
        if isinstance(value, dict):  # POSIX shape
            unprotected = list(value.get("unencrypted", []))
        elif isinstance(value, list):  # Windows shape
            unprotected = [
                str(v.get("MountPoint"))
                for v in value
                if isinstance(v, dict) and v.get("ProtectionStatus") in (0, "Off", False)
            ]

        if unprotected:
            finding = self.finding(
                f"{len(unprotected)} volume(s) are not encrypted",
                explanation=(
                    "If this machine is lost or stolen, the files on these volumes "
                    "can be read by removing the drive. Nothing has happened; this "
                    "is about what would happen."
                ),
                evidence={"unencrypted": unprotected, "source": reading.source},
                signals=Signals(weakness=True),
                base_severity=Severity.MEDIUM,
                remediation=(
                    "Turn on BitLocker for those drives. On a laptop this is the "
                    "single highest-value change on this list."
                ),
            )
        else:
            finding = self.finding(
                "Disks are encrypted",
                explanation="Volume contents are unreadable without the key.",
                evidence={"source": reading.source, "detail": value},
                signals=Signals(),
                base_severity=Severity.INFO,
            )

        return CheckResult(
            category=self.category,
            name=self.name,
            ran=True,
            findings=[finding],
            summary=f"Read volume encryption from {reading.source}.",
        )


class UpdatesCheck(SecurityCheck):
    category = Category.UPDATES
    name = "Updates"

    @_timed
    def run(self) -> CheckResult:
        reading = self.collector.updates()
        if not reading.known:
            return self.unavailable(reading)
        return CheckResult(
            category=self.category,
            name=self.name,
            ran=True,
            findings=[
                self.finding(
                    "Recent updates",
                    explanation="The most recent updates installed on this machine.",
                    evidence={"recent": reading.value, "source": reading.source},
                    signals=Signals(),
                    base_severity=Severity.INFO,
                )
            ],
            summary=f"Read update history from {reading.source}.",
        )


class StartupCheck(SecurityCheck):
    """What runs when the machine starts.

    The check where the "unfamiliar is not malicious" rule matters most: a
    startup list is long, mostly legitimate, and full of names nobody
    recognises. Flagging the unrecognised ones is exactly how a security tool
    becomes noise.
    """

    category = Category.STARTUP
    name = "Startup programs"

    @_timed
    def run(self) -> CheckResult:
        reading = self.collector.startup_items()
        if not reading.known:
            return self.unavailable(reading)

        items = reading.value if isinstance(reading.value, list) else []
        first_scan = not self.baseline.established()
        findings: list[Finding] = []
        new_items: list[dict[str, Any]] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("Name") or "")
            command = str(item.get("command") or item.get("Command") or "")
            location = str(item.get("location") or item.get("Location") or "")
            if not name:
                continue
            print_ = fingerprint(Category.STARTUP.value, name, command)
            novel = self.baseline.observe(print_, Category.STARTUP.value, name)
            trusted = self.baseline.is_trusted(print_)
            pattern = _volatile_path(command)

            if pattern:
                findings.append(
                    self.finding(
                        f"{name} starts from a temporary folder",
                        explanation=(
                            f"This runs at startup from {pattern}, which is where "
                            f"downloads and installers land. Legitimate software "
                            f"installs itself somewhere permanent, so this is worth "
                            f"looking at — it is not a malware verdict."
                        ),
                        evidence={
                            "name": name,
                            "command": command,
                            "location": location,
                            "source": reading.source,
                        },
                        signals=Signals(
                            matched_pattern=f"startup command under {pattern}",
                            novel=novel,
                            trusted=trusted,
                        ),
                        base_severity=Severity.MEDIUM,
                        remediation=(
                            "Check what this program is. If you do not recognise it, "
                            "disable it in Task Manager's Startup tab."
                        ),
                        print_fingerprint=print_,
                    )
                )
            elif novel and not first_scan:
                new_items.append({"name": name, "command": command, "location": location})

        if new_items:
            findings.append(
                self.finding(
                    f"{len(new_items)} new startup program(s) since the last scan",
                    explanation=novelty_note(True, "these startup programs")
                    + " Installing software usually adds one.",
                    evidence={"items": new_items, "source": reading.source},
                    signals=Signals(novel=True),
                    base_severity=Severity.INFO,
                )
            )

        if not findings:
            findings.append(
                self.finding(
                    f"{len(items)} startup program(s), all familiar"
                    if not first_scan
                    else f"{len(items)} startup program(s) recorded",
                    explanation=(
                        "First scan — Jarvis is learning what is normal on this "
                        "machine, so nothing here is reported as new."
                        if first_scan
                        else "Nothing has changed since the last scan."
                    ),
                    evidence={"count": len(items), "source": reading.source},
                    signals=Signals(),
                    base_severity=Severity.INFO,
                )
            )

        return CheckResult(
            category=self.category,
            name=self.name,
            ran=True,
            findings=findings,
            summary=f"Read {len(items)} startup item(s) from {reading.source}.",
        )


class NetworkCheck(SecurityCheck):
    category = Category.NETWORK
    name = "Network listeners"

    @_timed
    def run(self) -> CheckResult:
        reading = self.collector.listeners()
        if not reading.known:
            return self.unavailable(reading)

        listeners = reading.value if isinstance(reading.value, list) else []
        findings: list[Finding] = []
        external: list[dict[str, Any]] = []

        for listener in listeners:
            if not isinstance(listener, dict):
                continue
            port = int(listener.get("port") or 0)
            process = str(listener.get("process") or "")
            exe = str(listener.get("exe") or "")
            print_ = fingerprint(Category.NETWORK.value, process or str(port), str(port))
            novel = self.baseline.observe(print_, Category.NETWORK.value, f"{process}:{port}")
            trusted = self.baseline.is_trusted(print_)
            pattern = _volatile_path(exe)

            if pattern and listener.get("external"):
                findings.append(
                    self.finding(
                        f"{process or 'A program'} is accepting connections from a "
                        f"temporary folder",
                        explanation=(
                            f"A program running from {pattern} is listening on port "
                            f"{port} and reachable from the network. That combination "
                            f"is unusual for legitimate software, which installs "
                            f"itself somewhere permanent."
                        ),
                        evidence={**listener, "source": reading.source},
                        signals=Signals(
                            matched_pattern=f"external listener running from {pattern}",
                            novel=novel,
                            trusted=trusted,
                        ),
                        base_severity=Severity.HIGH,
                        remediation=(
                            "Find out what this program is before doing anything "
                            "else. If you do not recognise it, end it and check the "
                            "folder it is running from."
                        ),
                        print_fingerprint=print_,
                    )
                )
            elif listener.get("external") and port not in COMMON_LOCAL_PORTS:
                external.append(listener)

        if external:
            findings.append(
                self.finding(
                    f"{len(external)} program(s) reachable from the network",
                    explanation=(
                        "These accept connections from outside this machine. That is "
                        "normal for file sharing, remote desktop and development "
                        "servers — it is listed so you can see what is exposed, not "
                        "because anything is wrong."
                    ),
                    evidence={"listeners": external, "source": reading.source},
                    signals=Signals(),
                    base_severity=Severity.INFO,
                )
            )

        if not findings:
            findings.append(
                self.finding(
                    "Nothing is reachable from the network",
                    explanation=(
                        f"{len(listeners)} program(s) are listening, all of them only "
                        f"to this machine."
                    ),
                    evidence={"count": len(listeners), "source": reading.source},
                    signals=Signals(),
                    base_severity=Severity.INFO,
                )
            )

        return CheckResult(
            category=self.category,
            name=self.name,
            ran=True,
            findings=findings,
            summary=f"Read {len(listeners)} listening socket(s) from {reading.source}.",
        )


class DeviceCheck(SecurityCheck):
    category = Category.DEVICES
    name = "Removable devices"

    @_timed
    def run(self) -> CheckResult:
        reading = self.collector.removable_devices()
        if not reading.known:
            return self.unavailable(reading)

        devices = reading.value if isinstance(reading.value, list) else []
        new_devices: list[dict[str, Any]] = []
        first_scan = not self.baseline.established()
        for device in devices:
            if not isinstance(device, dict):
                continue
            label = str(device.get("model") or device.get("Model") or device.get("name") or "")
            serial = str(device.get("serialNumber") or device.get("SerialNumber") or "")
            print_ = fingerprint(Category.DEVICES.value, label, serial)
            if self.baseline.observe(print_, Category.DEVICES.value, label) and not first_scan:
                new_devices.append(device)

        finding = (
            self.finding(
                f"{len(new_devices)} removable device(s) not seen before",
                explanation=novelty_note(True, "these devices"),
                evidence={"devices": new_devices, "source": reading.source},
                signals=Signals(novel=True),
                base_severity=Severity.INFO,
            )
            if new_devices
            else self.finding(
                f"{len(devices)} removable device(s)",
                explanation="All of them have been attached to this machine before."
                if devices
                else "None are attached.",
                evidence={"count": len(devices), "source": reading.source},
                signals=Signals(),
                base_severity=Severity.INFO,
            )
        )
        return CheckResult(
            category=self.category,
            name=self.name,
            ran=True,
            findings=[finding],
            summary=f"Read {len(devices)} removable device(s) from {reading.source}.",
        )


def _volatile_path(command: str) -> str:
    """The temporary directory a command runs from, or an empty string."""
    lowered = (command or "").lower().replace('"', "")
    for marker in VOLATILE_PATHS:
        if marker in lowered:
            return marker.strip("\\/").replace("\\", "/")
    return ""


ALL_CHECKS: tuple[type[SecurityCheck], ...] = (
    AntivirusCheck,
    FirewallCheck,
    EncryptionCheck,
    UpdatesCheck,
    StartupCheck,
    NetworkCheck,
    DeviceCheck,
)

__all__ = [
    "ALL_CHECKS",
    "AntivirusCheck",
    "Classification",
    "DeviceCheck",
    "EncryptionCheck",
    "FirewallCheck",
    "NetworkCheck",
    "SecurityCheck",
    "StartupCheck",
    "UpdatesCheck",
]

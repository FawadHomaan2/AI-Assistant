"""Security types, and the one rule that matters most.

**Unfamiliar is not malicious.** This is the whole design constraint of the
Security Center, and it is why `Classification` has four values rather than a
"threat level" dial. A consumer security tool that flags everything it has not
seen before trains its user to dismiss it, and then the one real alert is
dismissed too.

So novelty on its own produces `NORMAL_ACTIVITY` with a note that it is new.
Only a *policy violation* or a *known-bad pattern* raises it, and `SUSPICIOUS`
requires a specific named reason that is written into the evidence. An
accusation Jarvis cannot justify in one sentence is one it does not make.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Classification(StrEnum):
    """What Jarvis is claiming. Deliberately not a severity scale."""

    #: Something definitely happened or is definitely true, and it is checkable:
    #: "Defender real-time protection is off", "Defender quarantined a file".
    CONFIRMED = "confirmed_event"
    #: A specific, nameable risky pattern — not merely something unrecognised.
    SUSPICIOUS = "suspicious_behavior"
    #: A weakness in configuration. Nothing has happened; something could.
    POTENTIAL = "potential_risk"
    #: Working as expected, including things that are simply new.
    NORMAL = "normal_activity"

    @property
    def label(self) -> str:
        return {
            Classification.CONFIRMED: "Confirmed",
            Classification.SUSPICIOUS: "Suspicious",
            Classification.POTENTIAL: "Potential risk",
            Classification.NORMAL: "Normal",
        }[self]

    @property
    def meaning(self) -> str:
        """Shown next to the badge, so the word cannot be misread."""
        return {
            Classification.CONFIRMED: "This definitely happened, and here is the evidence.",
            Classification.SUSPICIOUS: (
                "This matches a specific risky pattern, named in the evidence. "
                "It is not a malware verdict."
            ),
            Classification.POTENTIAL: "Nothing has happened; this makes something possible.",
            Classification.NORMAL: "Expected. Included so you can see what was checked.",
        }[self]


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return ["info", "low", "medium", "high", "critical"].index(self.value)


class Category(StrEnum):
    ANTIVIRUS = "antivirus"
    FIREWALL = "firewall"
    STARTUP = "startup"
    NETWORK = "network"
    ENCRYPTION = "encryption"
    UPDATES = "updates"
    ACCOUNTS = "accounts"
    DEVICES = "devices"


def fingerprint(*parts: str) -> str:
    """A stable id for a condition, so one problem is one row across scans."""
    joined = "\u001f".join(p.strip().lower() for p in parts if p)
    return hashlib.sha256(joined.encode()).hexdigest()[:32]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Finding:
    """One thing a check noticed, with the evidence behind it."""

    category: Category
    title: str
    classification: Classification
    severity: Severity
    #: Why Jarvis is saying this, in a sentence a non-expert can act on.
    explanation: str
    #: The measured facts. Shown in full; this is what makes a claim checkable.
    evidence: dict[str, Any] = field(default_factory=dict)
    #: What the user can do. Empty when there is genuinely nothing to do.
    remediation: str = ""
    #: True when this condition had not been seen before this scan.
    novel: bool = False
    fingerprint: str = ""
    id: str = ""
    status: str = "open"
    first_seen: str = ""
    last_seen: str = ""
    seen_count: int = 1

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = fingerprint(self.category.value, self.title)
        self.first_seen = self.first_seen or _now()
        self.last_seen = self.last_seen or self.first_seen

    @property
    def actionable(self) -> bool:
        return self.classification is not Classification.NORMAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category.value,
            "title": self.title,
            "classification": self.classification.value,
            "classificationLabel": self.classification.label,
            "classificationMeaning": self.classification.meaning,
            "severity": self.severity.value,
            "explanation": self.explanation,
            "evidence": self.evidence,
            "remediation": self.remediation,
            "novel": self.novel,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "firstSeen": self.first_seen,
            "lastSeen": self.last_seen,
            "seenCount": self.seen_count,
            "actionable": self.actionable,
        }


@dataclass
class CheckResult:
    """What one check produced, including why it could not run.

    A check that cannot run reports that it could not run. Returning no
    findings would read as "all clear", which is the single most dangerous
    thing a security tool can say when it has not actually looked.
    """

    category: Category
    name: str
    ran: bool
    findings: list[Finding] = field(default_factory=list)
    #: Set when `ran` is False: what is missing and what it would take.
    unavailable_reason: str = ""
    #: What was actually inspected, for the "what was checked" list.
    summary: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "name": self.name,
            "ran": self.ran,
            "summary": self.summary,
            "unavailableReason": self.unavailable_reason,
            "findings": [f.to_dict() for f in self.findings],
            "elapsedMs": self.elapsed_ms,
        }

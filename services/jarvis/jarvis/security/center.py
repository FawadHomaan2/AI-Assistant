"""The Security Center: run the checks, keep the findings, report the posture.

Two things it deliberately does not do.

**It never changes anything.** Every collector is a read. Turning Defender back
on, enabling the firewall and encrypting a drive are all things the user does,
in Windows' own interface, where Windows can ask for consent properly. An
assistant that could switch security controls off is a far better target than
one that reports on them.

**It never reports a posture it did not measure.** A scan where three checks
could not run says three checks could not run, and the overall summary counts
them. "Everything looks fine" when a third of the checks failed is the exact
failure this design exists to avoid.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jarvis.db.engine import Database
from jarvis.db.repositories import new_id
from jarvis.security.baseline import Baseline
from jarvis.security.checks import ALL_CHECKS, SecurityCheck
from jarvis.security.collectors import PostureCollector, collector
from jarvis.security.types import CheckResult, Classification, Finding, Severity
from jarvis.util.logging import get_logger

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class ScanReport:
    """One scan: what ran, what did not, and what was found."""

    started_at: str
    elapsed_ms: int
    results: list[CheckResult] = field(default_factory=list)
    first_scan: bool = False

    @property
    def findings(self) -> list[Finding]:
        return [f for r in self.results for f in r.findings]

    @property
    def actionable(self) -> list[Finding]:
        order = {
            Classification.SUSPICIOUS: 0,
            Classification.CONFIRMED: 1,
            Classification.POTENTIAL: 2,
            Classification.NORMAL: 3,
        }
        items = [f for f in self.findings if f.actionable]
        items.sort(key=lambda f: (order[f.classification], -f.severity.rank))
        return items

    @property
    def unavailable(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ran]

    def headline(self) -> str:
        """One sentence that never overstates what was actually checked."""
        checked = len(self.results) - len(self.unavailable)
        missed = len(self.unavailable)
        suffix = f" {missed} of {len(self.results)} checks could not run." if missed else ""

        worst = max(
            (f.severity.rank for f in self.actionable),
            default=-1,
        )
        if worst < 0:
            if checked == 0:
                return (
                    "Nothing could be checked on this machine, so Jarvis has no "
                    "idea what its security posture is." + suffix
                )
            return f"Nothing needs your attention across {checked} checks.{suffix}"

        counts = len(self.actionable)
        top = self.actionable[0]
        return (
            f"{counts} thing(s) worth your attention, the most important being "
            f'"{top.title}".{suffix}'
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "startedAt": self.started_at,
            "elapsedMs": self.elapsed_ms,
            "firstScan": self.first_scan,
            "headline": self.headline(),
            "checks": [r.to_dict() for r in self.results],
            "findings": [f.to_dict() for f in self.findings],
            "actionable": [f.to_dict() for f in self.actionable],
            "unavailable": [
                {"name": r.name, "category": r.category.value, "reason": r.unavailable_reason}
                for r in self.unavailable
            ],
            "checksRun": len(self.results) - len(self.unavailable),
            "checksTotal": len(self.results),
        }


class FindingsStore:
    """Findings, deduplicated by fingerprint.

    One condition is one row, updated across scans. Creating a new row each time
    would mean a single misconfigured setting produces a growing wall of
    identical alerts, which is how a user learns to stop reading them.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, finding: Finding) -> Finding:
        now = _now()
        existing = self.db.query_one(
            "SELECT id, first_seen, seen_count, status FROM security_findings"
            " WHERE fingerprint = ?",
            (finding.fingerprint,),
        )
        if existing is None:
            finding.id = new_id("find")
            finding.first_seen = now
            finding.last_seen = now
            self.db.execute(
                "INSERT INTO security_findings (id, detected_at, category, severity,"
                " classification, title, evidence_json, explanation, remediation,"
                " status, fingerprint, first_seen, last_seen, seen_count)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (
                    finding.id,
                    now,
                    finding.category.value,
                    finding.severity.value,
                    finding.classification.value,
                    finding.title,
                    _json(finding.evidence),
                    finding.explanation,
                    finding.remediation,
                    finding.status,
                    finding.fingerprint,
                    now,
                    now,
                ),
            )
            return finding

        finding.id = existing["id"]
        finding.first_seen = existing["first_seen"]
        finding.last_seen = now
        finding.seen_count = int(existing["seen_count"]) + 1
        # A condition that comes back after being resolved is open again: the
        # user fixed it and it returned, which is worth seeing.
        finding.status = "open" if existing["status"] == "resolved" else existing["status"]
        self.db.execute(
            "UPDATE security_findings SET detected_at = ?, severity = ?, classification = ?,"
            " evidence_json = ?, explanation = ?, remediation = ?, last_seen = ?,"
            " seen_count = ?, status = ? WHERE fingerprint = ?",
            (
                now,
                finding.severity.value,
                finding.classification.value,
                _json(finding.evidence),
                finding.explanation,
                finding.remediation,
                now,
                finding.seen_count,
                finding.status,
                finding.fingerprint,
            ),
        )
        return finding

    def resolve_missing(self, seen: set[str]) -> int:
        """Close findings that no longer reproduce.

        A fixed problem must stop being reported, or the panel never empties and
        the user cannot tell what is still true.
        """
        rows = self.db.query("SELECT fingerprint FROM security_findings WHERE status != 'resolved'")
        gone = [r["fingerprint"] for r in rows if r["fingerprint"] not in seen]
        for print_ in gone:
            self.db.execute(
                "UPDATE security_findings SET status = 'resolved' WHERE fingerprint = ?",
                (print_,),
            )
        return len(gone)

    def acknowledge(self, finding_id: str) -> bool:
        before = self.db.query_one("SELECT id FROM security_findings WHERE id = ?", (finding_id,))
        if before is None:
            return False
        self.db.execute(
            "UPDATE security_findings SET status = 'acknowledged', acknowledged_at = ?"
            " WHERE id = ?",
            (_now(), finding_id),
        )
        return True

    def open_findings(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM security_findings WHERE status != 'resolved'"
            " ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def count(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM security_findings WHERE status != 'resolved'"
        )
        return int(row["n"]) if row else 0


def _json(value: Any) -> str:
    import json

    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return "{}"


class SecurityCenter:
    def __init__(
        self,
        db: Database,
        posture: PostureCollector | None = None,
        checks: tuple[type[SecurityCheck], ...] = ALL_CHECKS,
    ) -> None:
        self.db = db
        self.baseline = Baseline(db)
        self.findings = FindingsStore(db)
        self.collector = posture or collector()
        self.check_types = checks
        self.last_scan: ScanReport | None = None

    def scan(self) -> ScanReport:
        """Run every check. One failing check never stops the others."""
        started = time.monotonic()
        first_scan = not self.baseline.established()
        results: list[CheckResult] = []

        for check_type in self.check_types:
            check = check_type(self.collector, self.baseline)
            try:
                results.append(check.run())
            except Exception as exc:  # a broken check must not hide the rest
                log.warning("security check failed", check=check_type.__name__, error=str(exc))
                results.append(
                    CheckResult(
                        category=check_type.category,
                        name=check_type.name,
                        ran=False,
                        unavailable_reason=(
                            f"This check failed to run: {exc}. The others completed."
                        ),
                        summary=f"{check_type.name} failed.",
                    )
                )

        report = ScanReport(
            started_at=_now(),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            results=results,
            first_scan=first_scan,
        )

        seen: set[str] = set()
        for finding in report.findings:
            if finding.actionable:
                self.findings.record(finding)
                seen.add(finding.fingerprint)
        self.findings.resolve_missing(seen)

        self.last_scan = report
        log.info(
            "security scan complete",
            checks=len(results),
            unavailable=len(report.unavailable),
            actionable=len(report.actionable),
            ms=report.elapsed_ms,
        )
        return report

    def status(self) -> dict[str, Any]:
        """Current posture without rescanning, for the panel on first paint."""
        return {
            "lastScan": self.last_scan.to_dict() if self.last_scan else None,
            "openFindings": self.findings.count(),
            "baselineItems": self.baseline.count(),
            "baselineEstablished": self.baseline.established(),
            "collector": self.collector.name,
            "neverChangesSettings": True,
        }


__all__ = ["FindingsStore", "ScanReport", "SecurityCenter", "Severity"]

"""Evidence-based diagnosis.

"Why is my computer slow?" has a bad answer and a good one. The bad answer is a
model guessing from the question. The good one is: measure the machine, apply
stated thresholds, and report each finding with the number that triggered it.

Every finding carries its evidence. If nothing crosses a threshold the answer is
"I measured these things and they look normal", with the measurements — not a
reassuring sentence and not an invented cause.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """How much a finding is likely to matter. Never 'this is the cause'."""

    CRITICAL = "critical"  # almost certainly causing a problem right now
    WARNING = "warning"  # likely contributing
    NOTICE = "notice"  # worth knowing, may be fine
    NORMAL = "normal"  # measured and within expectations


@dataclass
class Finding:
    severity: Severity
    title: str
    #: The measurement that produced this finding, in the user's terms.
    evidence: str
    #: What it probably means. Stated as likelihood, never as certainty.
    interpretation: str
    #: What the user could do, if anything.
    suggestion: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "title": self.title,
            "evidence": self.evidence,
            "interpretation": self.interpretation,
            "suggestion": self.suggestion,
            "data": self.data,
        }


# ── Thresholds, stated rather than buried ───────────────────────────────────
#: Sustained CPU above this means something is working hard.
CPU_BUSY = 80.0
CPU_SATURATED = 95.0
#: Memory pressure. Above the high mark Windows starts paging, which is the
#: single most common cause of a machine "feeling" slow.
MEMORY_HIGH = 85.0
MEMORY_CRITICAL = 95.0
#: Volumes smaller than this are boot, recovery or EFI partitions. They are
#: meant to be nearly full and reporting them as problems is pure noise.
DISK_MIN_INTERESTING_BYTES = 4 * 1024**3
#: Below this much free space Windows cannot manage its page file comfortably.
DISK_LOW_PERCENT = 90.0
DISK_CRITICAL_PERCENT = 95.0
DISK_LOW_FREE_BYTES = 2 * 1024**3
#: A single process using more than this share of RAM is worth naming.
PROCESS_MEMORY_SHARE = 0.20
#: Swap in active use means the machine has run out of real memory.
SWAP_ACTIVE = 10.0
#: Machines left on for weeks accumulate problems that a restart clears.
UPTIME_LONG_DAYS = 14


def _human(n: float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if size >= 10 else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def analyse_cpu(cpu: dict[str, Any], top_processes: list[dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    percent = float(cpu.get("percent") or 0.0)
    busiest = top_processes[0] if top_processes else None

    if percent >= CPU_SATURATED:
        findings.append(
            Finding(
                Severity.CRITICAL,
                "The processor is saturated",
                f"CPU is at {percent:.0f}% (measured over "
                f"{cpu.get('sampleSeconds', 0)}s, threshold {CPU_SATURATED:.0f}%)"
                + (
                    f"; the busiest program is {busiest['name']} at {busiest['cpuPercent']}%"
                    if busiest
                    else ""
                ),
                "At this level everything else has to wait for the processor, "
                "which is felt as general slowness.",
                (
                    f"Check whether {busiest['name']} should be working this hard."
                    if busiest
                    else "Check which program is responsible in the process list."
                ),
                {"percent": percent, "busiest": busiest},
            )
        )
    elif percent >= CPU_BUSY:
        findings.append(
            Finding(
                Severity.WARNING,
                "The processor is working hard",
                f"CPU is at {percent:.0f}% (threshold {CPU_BUSY:.0f}%)",
                "Sustained load at this level will make the machine feel less "
                "responsive, though short bursts are normal.",
                "If this persists, see which program is responsible.",
                {"percent": percent},
            )
        )
    else:
        findings.append(
            Finding(
                Severity.NORMAL,
                "Processor load is normal",
                f"CPU is at {percent:.0f}%, below the {CPU_BUSY:.0f}% threshold",
                "The processor is not the bottleneck.",
                data={"percent": percent},
            )
        )
    return findings


def analyse_memory(memory: dict[str, Any], top_processes: list[dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    percent = float(memory.get("percent") or 0.0)
    total = int(memory.get("totalBytes") or 0)
    swap_percent = float(memory.get("swapPercent") or 0.0)

    if percent >= MEMORY_CRITICAL:
        findings.append(
            Finding(
                Severity.CRITICAL,
                "Memory is nearly full",
                f"{percent:.0f}% of {_human(total)} is in use (threshold {MEMORY_CRITICAL:.0f}%)",
                "When memory fills, Windows moves data to disk to cope. That is "
                "far slower than RAM and is the most common reason a computer "
                "feels sluggish.",
                "Close programs you are not using, starting with the largest.",
                {"percent": percent},
            )
        )
    elif percent >= MEMORY_HIGH:
        findings.append(
            Finding(
                Severity.WARNING,
                "Memory is under pressure",
                f"{percent:.0f}% of {_human(total)} is in use (threshold {MEMORY_HIGH:.0f}%)",
                "There is little headroom left; opening anything large may push "
                "the machine into paging.",
                "Closing a few programs would give it room.",
                {"percent": percent},
            )
        )
    else:
        findings.append(
            Finding(
                Severity.NORMAL,
                "Memory use is normal",
                f"{percent:.0f}% of {_human(total)} is in use",
                "There is enough free memory.",
                data={"percent": percent},
            )
        )

    if swap_percent >= SWAP_ACTIVE:
        findings.append(
            Finding(
                Severity.WARNING,
                "The machine is paging to disk",
                f"Swap is {swap_percent:.0f}% used (threshold {SWAP_ACTIVE:.0f}%)",
                "Paging means real memory ran out at some point. Disk is orders "
                "of magnitude slower than RAM, so this is felt directly.",
                "Close memory-heavy programs, or consider more RAM if it is constant.",
                {"swapPercent": swap_percent},
            )
        )

    # Name a single program only when it is genuinely dominant.
    if total:
        for process in top_processes[:5]:
            share = int(process.get("memoryBytes") or 0) / total
            if share >= PROCESS_MEMORY_SHARE:
                findings.append(
                    Finding(
                        Severity.NOTICE,
                        f"{process['name']} is using a large share of memory",
                        f"{process['name']} (pid {process['pid']}) holds "
                        f"{_human(process['memoryBytes'])}, {share * 100:.0f}% of "
                        f"total RAM (threshold {PROCESS_MEMORY_SHARE * 100:.0f}%)",
                        "That may be entirely normal for this program — browsers "
                        "and editors legitimately use a lot — but it is the "
                        "largest single consumer.",
                        "Worth a look if you are not actively using it.",
                        {"process": process, "share": share},
                    )
                )
    return findings


def analyse_disks(disks: list[dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    for disk in disks:
        if not disk.get("readable"):
            findings.append(
                Finding(
                    Severity.NOTICE,
                    f"Could not read {disk.get('mountpoint')}",
                    "The volume exists but its usage could not be measured.",
                    "This is usually a permissions or removable-media issue, not a fault.",
                    data={"disk": disk},
                )
            )
            continue

        # A read-only volume at 100% is working as intended, and a small one is
        # a system partition. Neither explains a slow computer.
        if disk.get("writable") is False:
            continue
        if int(disk.get("totalBytes") or 0) < DISK_MIN_INTERESTING_BYTES:
            continue

        percent = float(disk.get("percent") or 0.0)
        free = int(disk.get("freeBytes") or 0)
        mount = disk.get("mountpoint")

        if percent >= DISK_CRITICAL_PERCENT or free < DISK_LOW_FREE_BYTES:
            findings.append(
                Finding(
                    Severity.CRITICAL,
                    f"{mount} is almost full",
                    f"{percent:.0f}% used, {_human(free)} free "
                    f"(thresholds {DISK_CRITICAL_PERCENT:.0f}% / {_human(DISK_LOW_FREE_BYTES)})",
                    "Windows needs free space for its page file and temporary "
                    "files. Below a couple of gigabytes it starts to struggle, "
                    "and updates can fail.",
                    "Freeing space here is likely to help more than anything else.",
                    {"disk": disk},
                )
            )
        elif percent >= DISK_LOW_PERCENT:
            findings.append(
                Finding(
                    Severity.WARNING,
                    f"{mount} is running low on space",
                    f"{percent:.0f}% used, {_human(free)} free (threshold {DISK_LOW_PERCENT:.0f}%)",
                    "Not yet a problem, but worth clearing before it becomes one.",
                    "Emptying the Recycle Bin and Downloads is usually the easiest win.",
                    {"disk": disk},
                )
            )
    if not any(f.severity is not Severity.NOTICE for f in findings):
        readable = [
            d
            for d in disks
            if d.get("readable")
            and d.get("writable") is not False
            and int(d.get("totalBytes") or 0) >= DISK_MIN_INTERESTING_BYTES
        ]
        if readable:
            findings.append(
                Finding(
                    Severity.NORMAL,
                    "Disk space is fine",
                    "; ".join(
                        f"{d['mountpoint']} {d['percent']:.0f}% used, {d['freeHuman']} free"
                        for d in readable[:4]
                    ),
                    "No volume is close to full.",
                )
            )
    return findings


def analyse_uptime(uptime: dict[str, Any]) -> list[Finding]:
    days = int(uptime.get("seconds") or 0) // 86400
    if days >= UPTIME_LONG_DAYS:
        return [
            Finding(
                Severity.NOTICE,
                "This computer has been running for a long time",
                f"Up for {uptime.get('human')} (threshold {UPTIME_LONG_DAYS} days)",
                "Long uptimes accumulate memory fragmentation and leaked handles. "
                "This is not a fault, but a restart often helps noticeably.",
                "Restarting when convenient is worth trying.",
                {"days": days},
            )
        ]
    return []


def summarise(findings: list[Finding]) -> str:
    """One honest sentence about what was found."""
    serious = [f for f in findings if f.severity in (Severity.CRITICAL, Severity.WARNING)]
    if not serious:
        return (
            "I measured the processor, memory, disks and uptime, and none of them "
            "crossed the thresholds that usually explain slowness. Whatever you are "
            "noticing is not visible in these numbers right now."
        )
    worst = [f for f in findings if f.severity is Severity.CRITICAL]
    lead = worst[0] if worst else serious[0]
    others = len(serious) - 1
    return f"The most likely cause is: {lead.title.lower()}. {lead.evidence}." + (
        f" {others} other thing(s) also look worth attention." if others else ""
    )


def analyse(snapshot: dict[str, Any]) -> list[Finding]:
    """Run every analyser over a collected snapshot."""
    findings: list[Finding] = []
    top = snapshot.get("topProcesses") or []
    findings += analyse_cpu(snapshot.get("cpu") or {}, top)
    findings += analyse_memory(snapshot.get("memory") or {}, top)
    findings += analyse_disks(snapshot.get("disks") or [])
    findings += analyse_uptime(snapshot.get("uptime") or {})
    order = {
        Severity.CRITICAL: 0,
        Severity.WARNING: 1,
        Severity.NOTICE: 2,
        Severity.NORMAL: 3,
    }
    findings.sort(key=lambda f: order[f.severity])
    return findings

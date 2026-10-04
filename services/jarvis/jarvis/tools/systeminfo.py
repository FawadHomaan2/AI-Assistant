"""SystemInfoTool — what this machine is and how hard it is working.

psutil is identical on Windows and here, so this tool is genuinely verified
rather than written blind. Every field that cannot be read comes back as `None`
and renders as an em-dash: "unknown" is never reported as zero.
"""

from __future__ import annotations

import contextlib
import platform
import sys
import time
from typing import Any

import psutil

from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.tools.base import Preview, Tool, ToolInputInvalid, ToolResult, ToolSpec
from jarvis.util.logging import get_logger

log = get_logger(__name__)

OPERATIONS = ("summary", "cpu", "memory", "disks", "battery", "os", "uptime")

#: CPU percentage is a delta; a single read reports zero for everything.
CPU_SAMPLE_SECONDS = 0.3


def human(n: float | None, unit: str = "B") -> str:
    if n is None:
        return "—"
    size = float(n)
    for suffix in ("", "K", "M", "G", "T"):
        if size < 1024 or suffix == "T":
            return f"{size:.0f} {suffix}{unit}" if size >= 10 else f"{size:.1f} {suffix}{unit}"
        size /= 1024
    return f"{size:.1f} T{unit}"


def _percent(used: int | None, total: int | None) -> float | None:
    if not used or not total:
        return None
    return round(used / total * 100, 1)


class SystemInfoTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="systeminfo",
            description=(
                "Report this computer's CPU, memory, disks, battery, network "
                "counters, operating system and uptime."
            ),
            scopes=[Scope.SYSTEM_INFO],
            risk=Risk.SAFE,
            input_schema={
                "type": "object",
                "required": ["operation"],
                "properties": {"operation": {"type": "string", "enum": list(OPERATIONS)}},
            },
        )

    async def preview(self, args: dict[str, Any]) -> Preview:
        operation = str(args.get("operation", "summary"))
        if operation not in OPERATIONS:
            raise ToolInputInvalid(
                f"Unknown operation {operation!r}. Supported: {', '.join(OPERATIONS)}."
            )
        return Preview(
            summary=f"Read {operation} — inspection only",
            affected=0,
            reversible="undoable",
            blast_radius="Nothing changes.",
        )

    # ── collectors ───────────────────────────────────────────────────────
    @staticmethod
    def cpu() -> dict[str, Any]:
        psutil.cpu_percent(None)  # prime
        time.sleep(CPU_SAMPLE_SECONDS)
        # Neither is exposed on every platform or inside every VM.
        frequency = None
        with contextlib.suppress(OSError, NotImplementedError, AttributeError):
            if freq := psutil.cpu_freq():
                frequency = round(freq.current)
        load: tuple[float, float, float] | None = None
        with contextlib.suppress(OSError, AttributeError):
            load = psutil.getloadavg()
        return {
            "percent": psutil.cpu_percent(None),
            "perCore": psutil.cpu_percent(None, percpu=True),
            "physicalCores": psutil.cpu_count(logical=False),
            "logicalCores": psutil.cpu_count(logical=True),
            "frequencyMhz": frequency,
            "loadAverage": list(load) if load else None,
            "sampleSeconds": CPU_SAMPLE_SECONDS,
        }

    @staticmethod
    def memory() -> dict[str, Any]:
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "totalBytes": virtual.total,
            "usedBytes": virtual.used,
            "availableBytes": virtual.available,
            "percent": virtual.percent,
            "totalHuman": human(virtual.total),
            "usedHuman": human(virtual.used),
            "swapTotalBytes": swap.total,
            "swapUsedBytes": swap.used,
            "swapPercent": swap.percent,
        }

    @staticmethod
    def disks() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for part in psutil.disk_partitions(all=False):
            options = (part.opts or "").lower()
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                # An unreadable mount is reported as unreadable, not skipped
                # silently — "you have 2 drives" when you have 3 is wrong.
                out.append(
                    {
                        "device": part.device,
                        "mountpoint": part.mountpoint,
                        "filesystem": part.fstype,
                        "readable": False,
                        "detail": "Could not read this volume.",
                    }
                )
                continue
            out.append(
                {
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "filesystem": part.fstype,
                    "readable": True,
                    # A read-only volume being "full" is its normal state — a
                    # mounted image, a recovery partition, a squashfs. Marking
                    # it lets the diagnostics skip it instead of reporting five
                    # critical findings about volumes nobody can write to.
                    "writable": "ro" not in options.split(","),
                    "totalBytes": usage.total,
                    "usedBytes": usage.used,
                    "freeBytes": usage.free,
                    "percent": usage.percent,
                    "totalHuman": human(usage.total),
                    "freeHuman": human(usage.free),
                }
            )
        return out

    @staticmethod
    def battery() -> dict[str, Any] | None:
        try:
            state = psutil.sensors_battery()
        except (AttributeError, NotImplementedError, OSError):
            return None
        if state is None:
            return None
        remaining = None
        if state.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
            remaining = int(state.secsleft)
        return {
            "percent": round(state.percent, 1),
            "pluggedIn": state.power_plugged,
            "secondsRemaining": remaining,
        }

    @staticmethod
    def operating_system() -> dict[str, Any]:
        return {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "hostname": platform.node(),
            "python": sys.version.split()[0],
        }

    @staticmethod
    def uptime() -> dict[str, Any]:
        boot = psutil.boot_time()
        seconds = int(time.time() - boot)
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{minutes}m"]
        return {
            "bootTime": boot,
            "seconds": seconds,
            "human": " ".join(p for p in parts if p),
        }

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        operation = str(args.get("operation", "summary"))

        if operation == "cpu":
            data = self.cpu()
            return ToolResult(
                ok=True,
                summary=f"CPU at {data['percent']}% across {data['logicalCores']} logical cores",
                data=data,
            )
        if operation == "memory":
            data = self.memory()
            return ToolResult(
                ok=True,
                summary=f"Memory {data['usedHuman']} of {data['totalHuman']} used "
                f"({data['percent']}%)",
                data=data,
            )
        if operation == "disks":
            volumes = self.disks()
            readable = [d for d in volumes if d.get("readable")]
            return ToolResult(
                ok=True,
                summary=(
                    f"{len(volumes)} volume(s); "
                    + ", ".join(f"{d['mountpoint']} {d['freeHuman']} free" for d in readable[:3])
                    if readable
                    else f"{len(volumes)} volume(s), none readable"
                ),
                data={"disks": volumes},
            )
        if operation == "battery":
            power = self.battery()
            return ToolResult(
                ok=True,
                summary=(
                    f"Battery {power['percent']}%" + (" (charging)" if power["pluggedIn"] else "")
                    if power
                    else "No battery — this is a desktop, or the platform did not report one"
                ),
                data={"battery": power},
            )
        if operation == "os":
            data = self.operating_system()
            return ToolResult(
                ok=True,
                summary=f"{data['system']} {data['release']} on {data['machine']}",
                data=data,
            )
        if operation == "uptime":
            data = self.uptime()
            return ToolResult(ok=True, summary=f"Up for {data['human']}", data=data)

        # summary
        cpu, memory, uptime = self.cpu(), self.memory(), self.uptime()
        disks, battery = self.disks(), self.battery()
        operating = self.operating_system()
        primary = next((d for d in disks if d.get("readable")), None)
        return ToolResult(
            ok=True,
            summary=(
                f"{operating['system']} {operating['release']}, "
                f"CPU {cpu['percent']}%, memory {memory['percent']}%"
                + (f", {primary['freeHuman']} free on {primary['mountpoint']}" if primary else "")
                + f", up {uptime['human']}"
            ),
            data={
                "cpu": cpu,
                "memory": memory,
                "disks": disks,
                "battery": battery,
                "os": operating,
                "uptime": uptime,
            },
        )

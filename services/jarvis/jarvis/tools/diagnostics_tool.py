"""DiagnosticsTool — "why is my computer slow?", answered with measurements.

Collects a snapshot, runs it through the thresholds in `jarvis.diagnostics`, and
reports each finding with the number that produced it. The model is not asked to
diagnose anything; it is given measured findings to relay.
"""

from __future__ import annotations

from typing import Any

from jarvis.diagnostics.analyzer import Severity, analyse, summarise
from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.platform_.base import ProcessBackend
from jarvis.tools.base import Preview, Tool, ToolInputInvalid, ToolResult, ToolSpec
from jarvis.tools.network import NetworkTool
from jarvis.tools.systeminfo import SystemInfoTool
from jarvis.util.logging import get_logger

log = get_logger(__name__)

OPERATIONS = ("performance", "connectivity", "snapshot")


class DiagnosticsTool(Tool):
    def __init__(self, processes: ProcessBackend) -> None:
        self.processes = processes
        self.system = SystemInfoTool()
        self.network = NetworkTool()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="diagnostics",
            description=(
                "Work out why this computer is slow, or why the internet is not "
                "working, by measuring it and reporting what crossed a threshold."
            ),
            scopes=[Scope.SYSTEM_INFO, Scope.PROCESS_READ],
            risk=Risk.SAFE,
            input_schema={
                "type": "object",
                "required": ["operation"],
                "properties": {"operation": {"type": "string", "enum": list(OPERATIONS)}},
            },
        )

    async def preview(self, args: dict[str, Any]) -> Preview:
        operation = str(args.get("operation", "performance"))
        if operation not in OPERATIONS:
            raise ToolInputInvalid(
                f"Unknown operation {operation!r}. Supported: {', '.join(OPERATIONS)}."
            )
        return Preview(
            summary=f"Run {operation} diagnostics — measurement only",
            affected=0,
            reversible="undoable",
            blast_radius="Nothing changes. This reads the machine's state.",
        )

    def _snapshot(self) -> dict[str, Any]:
        """Measure everything the analyser needs, once."""
        import psutil

        # CPU per process needs two samples; take them around the other reads so
        # the interval is spent usefully rather than sleeping twice.
        processes = []
        for proc in psutil.process_iter():
            try:
                proc.cpu_percent(None)
                processes.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        cpu = self.system.cpu()  # this sleeps for the sample interval
        memory = self.system.memory()
        disks = self.system.disks()
        uptime = self.system.uptime()

        measured: list[dict[str, Any]] = []
        for proc in processes:
            try:
                info = self.processes.get(proc.pid)
                if info is None:
                    continue
                info.cpu_percent = proc.cpu_percent(None)
                measured.append(info.to_dict())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        measured.sort(key=lambda p: (float(p["cpuPercent"]), p["memoryBytes"]), reverse=True)

        return {
            "cpu": cpu,
            "memory": memory,
            "disks": disks,
            "uptime": uptime,
            "topProcesses": measured[:10],
        }

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        operation = str(args.get("operation", "performance"))

        if operation == "connectivity":
            result = self.network.connectivity()
            interfaces = self.network.interfaces()
            up = [i["name"] for i in interfaces if i["up"]]
            return ToolResult(
                ok=True,
                summary=str(result["verdict"]),
                data={
                    "connectivity": result,
                    "interfacesUp": up,
                    "evidence": [
                        f"TCP connection to 1.1.1.1:443: "
                        f"{'succeeded' if result['reachable'] else 'failed'}"
                        + (f" in {result['latencyMs']}ms" if result.get("latencyMs") else ""),
                        f"DNS lookup: {'succeeded' if result['dns'] else 'failed'}",
                        f"Interfaces up: {', '.join(up) or 'none'}",
                    ],
                },
            )

        snapshot = self._snapshot()
        if operation == "snapshot":
            return ToolResult(
                ok=True,
                summary="Collected a full system snapshot",
                data=snapshot,
            )

        findings = analyse(snapshot)
        serious = [f for f in findings if f.severity in (Severity.CRITICAL, Severity.WARNING)]
        log.info(
            "performance diagnosis",
            findings=len(findings),
            critical=sum(1 for f in findings if f.severity is Severity.CRITICAL),
        )
        return ToolResult(
            ok=True,
            summary=summarise(findings),
            data={
                "findings": [f.to_dict() for f in findings],
                "seriousCount": len(serious),
                "snapshot": snapshot,
                # Stated so the model relaying this cannot imply more certainty
                # than the measurements support.
                "note": (
                    "Each finding names the measurement and threshold that "
                    "produced it. Nothing here is a guess; equally, a cause that "
                    "did not cross a threshold will not appear."
                ),
            },
        )

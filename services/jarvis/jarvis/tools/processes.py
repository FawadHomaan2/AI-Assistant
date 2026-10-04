"""ProcessTool — see what is running, and end it when asked.

psutil is genuinely cross-platform, so this tool behaves identically on Windows
and on the development machine. That is unusual for Phase 4 and worth stating:
most of this is verified for real, not mocked.

Ending a process is tiered by how it is done. A graceful request (`terminate`,
which on Windows posts WM_CLOSE to the process's windows) lets the application
save and prompt. A force kill does not, and loses unsaved work, so it is a tier
higher. Protected processes are refused outright, below the policy engine, where
no approval can reach them.
"""

from __future__ import annotations

import time
from typing import Any

from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.platform_.base import ProcessBackend
from jarvis.platform_.protected import check as check_protected
from jarvis.tools.base import Preview, Tool, ToolError, ToolInputInvalid, ToolResult, ToolSpec
from jarvis.util.logging import get_logger

log = get_logger(__name__)

OPERATIONS = ("list", "find", "info", "top", "end")

#: How long a measured CPU sample takes. Short enough not to stall a turn,
#: long enough to mean something.
CPU_SAMPLE_SECONDS = 0.35


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if size >= 10 or unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class ProcessTool(Tool):
    def __init__(self, backend: ProcessBackend) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="process",
            description=(
                "List running programs, find one by name, see what is using CPU "
                "or memory, and end a program when asked. Core Windows processes "
                "and security software are never ended."
            ),
            scopes=[Scope.PROCESS_READ],
            risk=Risk.SAFE,
            input_schema={
                "type": "object",
                "required": ["operation"],
                "properties": {
                    "operation": {"type": "string", "enum": list(OPERATIONS)},
                    "name": {"type": "string", "description": "Program name, for find/end"},
                    "pid": {"type": "integer"},
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Kill without letting the program save first",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
            },
        )

    def risk_for(self, args: dict[str, Any], preview: Preview) -> Risk:
        del preview
        if str(args.get("operation")) != "end":
            return Risk.SAFE
        # A force kill discards unsaved work; a graceful close does not.
        return Risk.HIGH if args.get("force") else Risk.MEDIUM

    def scopes_for(self, args: dict[str, Any]) -> list[Scope]:
        if str(args.get("operation")) == "end":
            return [Scope.PROCESS_READ, Scope.PROCESS_KILL]
        return [Scope.PROCESS_READ]

    # ── resolution ───────────────────────────────────────────────────────
    def _targets(self, args: dict[str, Any]) -> list[Any]:
        """Which processes an `end` refers to."""
        if pid := args.get("pid"):
            found = self.backend.get(int(pid))
            return [found] if found else []
        name = str(args.get("name") or "").strip()
        if not name:
            raise ToolInputInvalid("Ending a program needs a name or a pid.")
        return self.backend.find(name)

    async def preview(self, args: dict[str, Any]) -> Preview:
        operation = str(args.get("operation", ""))
        if operation not in OPERATIONS:
            raise ToolInputInvalid(
                f"Unknown operation {operation!r}. Supported: {', '.join(OPERATIONS)}."
            )

        if operation != "end":
            return Preview(
                summary=f"{operation} running programs — inspection only",
                affected=0,
                reversible="undoable",
                blast_radius="Nothing changes.",
            )

        targets = self._targets(args)
        if not targets:
            label = args.get("name") or f"pid {args.get('pid')}"
            return Preview(
                summary=f"{label} is not running",
                affected=0,
                reversible="undoable",
                blast_radius="Nothing changes.",
                blocked=f"No running program matches {label}.",
            )

        if protected := [p for p in targets if p.protected]:
            first = protected[0]
            return Preview(
                summary=f"Cannot end {first.name}",
                targets=[f"{p.name} (pid {p.pid})" for p in protected],
                affected=len(protected),
                reversible="permanent",
                blast_radius="Nothing changes.",
                blocked=(
                    f"Jarvis will not end {first.name} because {first.protection_reason}. "
                    f"This is not something a confirmation can override."
                ),
            )

        force = bool(args.get("force"))
        total_memory = sum(p.memory_bytes for p in targets)
        return Preview(
            summary=(
                f"{'Force-kill' if force else 'Close'} "
                f"{len(targets)} process{'es' if len(targets) != 1 else ''}: "
                f"{', '.join(sorted({p.name for p in targets}))}"
            ),
            targets=[f"{p.name} (pid {p.pid}, {_human_bytes(p.memory_bytes)})" for p in targets],
            affected=len(targets),
            reversible="permanent",
            blast_radius=(
                f"{len(targets)} running program{'s' if len(targets) != 1 else ''}, "
                f"{_human_bytes(total_memory)} of memory. "
                + (
                    "A force kill gives the program no chance to save — unsaved work "
                    "in it will be lost."
                    if force
                    else "The program is asked to close and can prompt you to save."
                )
            ),
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        operation = str(args.get("operation", ""))
        handler = getattr(self, f"_do_{operation}", None)
        if handler is None:
            raise ToolInputInvalid(f"Unsupported operation {operation!r}.")
        return await handler(args)  # type: ignore[no-any-return]

    async def _do_list(self, args: dict[str, Any]) -> ToolResult:
        limit = int(args.get("limit") or 20)
        processes = self.backend.list_all()[:limit]
        return ToolResult(
            ok=True,
            summary=f"{len(processes)} running programs (largest first)",
            data={"processes": [p.to_dict() for p in processes]},
        )

    async def _do_find(self, args: dict[str, Any]) -> ToolResult:
        name = str(args.get("name") or "")
        matches = self.backend.find(name)
        return ToolResult(
            ok=True,
            summary=(
                f"{len(matches)} process{'es' if len(matches) != 1 else ''} matching {name!r}"
                if matches
                else f"Nothing running matches {name!r}"
            ),
            data={"query": name, "processes": [p.to_dict() for p in matches]},
        )

    async def _do_info(self, args: dict[str, Any]) -> ToolResult:
        pid = args.get("pid")
        if pid is None:
            raise ToolInputInvalid("`info` needs a pid.")
        found = self.backend.get(int(pid))
        if found is None:
            raise ToolError(f"No process with pid {pid}.")
        return ToolResult(
            ok=True,
            summary=f"{found.name} (pid {found.pid}), {_human_bytes(found.memory_bytes)}",
            data=found.to_dict(),
        )

    async def _do_top(self, args: dict[str, Any]) -> ToolResult:
        """What is actually using the CPU right now.

        CPU percentage is a delta between two samples, so a single reading is
        meaningless. This takes a real interval rather than reporting the zeros
        a one-shot read would give.
        """
        import psutil

        limit = int(args.get("limit") or 10)
        procs = []
        for proc in psutil.process_iter():
            try:
                proc.cpu_percent(None)  # prime the counter
                procs.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        time.sleep(CPU_SAMPLE_SECONDS)

        measured: list[dict[str, Any]] = []
        for proc in procs:
            try:
                info = self.backend.get(proc.pid)
                if info is None:
                    continue
                info.cpu_percent = proc.cpu_percent(None)
                measured.append(info.to_dict())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        measured.sort(key=lambda p: float(p["cpuPercent"]), reverse=True)
        top = measured[:limit]
        return ToolResult(
            ok=True,
            summary=(
                f"Busiest: {top[0]['name']} at {top[0]['cpuPercent']}% CPU"
                if top
                else "No processes could be measured"
            ),
            data={"processes": top, "sampleSeconds": CPU_SAMPLE_SECONDS},
        )

    async def _do_end(self, args: dict[str, Any]) -> ToolResult:
        targets = self._targets(args)
        if not targets:
            raise ToolError("Nothing matching is running.")

        force = bool(args.get("force"))
        ended: list[str] = []
        failed: list[str] = []
        for target in targets:
            # Re-checked at the point of action: the list was built earlier and
            # a pid can be reused.
            protected, reason = check_protected(target.pid, target.name, target.exe)
            if protected:
                raise ToolError(f"Jarvis will not end {target.name} because {reason}.")
            try:
                if self.backend.terminate(target.pid, force=force):
                    ended.append(f"{target.name} (pid {target.pid})")
                else:
                    failed.append(f"{target.name} (pid {target.pid}) did not exit in time")
            except Exception as exc:  # surfaced, never swallowed
                failed.append(f"{target.name} (pid {target.pid}): {exc}")

        log.info("processes ended", count=len(ended), forced=force, failed=len(failed))
        return ToolResult(
            ok=bool(ended) and not failed,
            summary=(
                f"Ended {len(ended)} process{'es' if len(ended) != 1 else ''}"
                + (f"; {len(failed)} could not be ended" if failed else "")
            ),
            data={"ended": ended, "failed": failed, "forced": force},
            changes=[f"Ended {e}" for e in ended],
            error="; ".join(failed),
        )

    async def observe(self, args: dict[str, Any]) -> dict[str, Any]:
        """Whether the targets are still running, so the result can be verified."""
        if str(args.get("operation")) != "end":
            return {}
        try:
            return {"running": sorted(p.pid for p in self._targets(args))}
        except Exception:
            return {}

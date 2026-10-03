"""ApplicationTool — start programs by name.

The security property worth stating plainly: **the model never supplies a path
to execute.** It supplies a name, which is matched against software this machine
has installed, and the catalogue entry supplies the launch target. A document
that says "open C:\\Users\\me\\Downloads\\invoice.pdf.exe" therefore cannot turn
into a launch, because no catalogue entry matches it.

That is why there is no `path` input on this tool. Adding one would hand a
prompt-injected model a way to run an arbitrary binary, and no amount of
confirmation UI makes that a good trade.
"""

from __future__ import annotations

from typing import Any

from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.platform_ import app_catalog
from jarvis.platform_.base import AppBackend, ProcessBackend
from jarvis.tools.base import Preview, Tool, ToolError, ToolInputInvalid, ToolResult, ToolSpec
from jarvis.util.logging import get_logger

log = get_logger(__name__)

OPERATIONS = ("launch", "list", "find")

#: Shown when a name matches several installed programs.
MAX_SUGGESTIONS = 6


class AppAmbiguous(ToolError):
    code = "jarvis.app.ambiguous"
    http_status = 409


class AppNotFound(ToolError):
    code = "jarvis.app.not_found"
    http_status = 404


class ApplicationTool(Tool):
    def __init__(self, apps: AppBackend, processes: ProcessBackend) -> None:
        self.apps = apps
        self.processes = processes

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="application",
            description=(
                "Start an installed program by name, and list what is installed. "
                "Only software found on this computer can be started — Jarvis "
                "never runs a path it was handed."
            ),
            scopes=[Scope.APP_LAUNCH],
            risk=Risk.LOW,
            input_schema={
                "type": "object",
                "required": ["operation"],
                "properties": {
                    "operation": {"type": "string", "enum": list(OPERATIONS)},
                    "name": {"type": "string", "description": "What the program is called"},
                    "limit": {"type": "integer", "default": 40},
                },
            },
        )

    def risk_for(self, args: dict[str, Any], preview: Preview) -> Risk:
        del preview
        return Risk.LOW if str(args.get("operation")) == "launch" else Risk.SAFE

    def scopes_for(self, args: dict[str, Any]) -> list[Scope]:
        if str(args.get("operation")) == "launch":
            return [Scope.APP_LAUNCH]
        return [Scope.APP_LAUNCH]

    def _resolve(self, name: str) -> Any:
        """Find exactly one installed program, or explain why not."""
        catalogue = self.apps.catalogue()
        chosen, matches = app_catalog.pick(name, catalogue)
        if chosen is not None:
            return chosen
        if not matches:
            raise AppNotFound(
                f"No installed program called {name!r} was found on this computer. "
                f"Jarvis only starts software it can see is installed."
            )
        names = ", ".join(m.app.name for m in matches[:MAX_SUGGESTIONS])
        raise AppAmbiguous(
            f"{name!r} matches several installed programs: {names}. Which one did you mean?"
        )

    async def preview(self, args: dict[str, Any]) -> Preview:
        operation = str(args.get("operation", ""))
        if operation not in OPERATIONS:
            raise ToolInputInvalid(
                f"Unknown operation {operation!r}. Supported: {', '.join(OPERATIONS)}."
            )

        if operation != "launch":
            return Preview(
                summary=f"{operation} installed programs — inspection only",
                affected=0,
                reversible="undoable",
                blast_radius="Nothing changes.",
            )

        self.require(args, "name")
        try:
            app = self._resolve(str(args["name"]))
        except ToolError as exc:
            return Preview(
                summary=f"Cannot start {args['name']}",
                blocked=exc.message,
                reversible="undoable",
                blast_radius="Nothing changes.",
            )

        running = self.processes.find(app.key)
        return Preview(
            summary=f"Start {app.name}",
            targets=[app.name],
            affected=1,
            reversible="undoable",
            blast_radius=(
                f"{app.name} is already running; starting it may open another window."
                if running
                else f"Opens {app.name}. Nothing on disk changes."
            ),
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        operation = str(args.get("operation", ""))

        if operation == "list":
            catalogue = self.apps.catalogue()[: int(args.get("limit") or 40)]
            return ToolResult(
                ok=True,
                summary=f"{len(catalogue)} installed programs Jarvis can start",
                data={
                    "apps": [a.to_dict() for a in catalogue],
                    "source": self.apps.describe(),
                },
            )

        if operation == "find":
            self.require(args, "name")
            matches = app_catalog.resolve(str(args["name"]), self.apps.catalogue())
            return ToolResult(
                ok=True,
                summary=f"{len(matches)} program(s) matching {args['name']!r}",
                data={
                    "matches": [
                        {**m.app.to_dict(), "score": round(m.score, 2), "reason": m.reason}
                        for m in matches[:MAX_SUGGESTIONS]
                    ]
                },
            )

        if operation != "launch":
            raise ToolInputInvalid(f"Unsupported operation {operation!r}.")

        self.require(args, "name")
        app = self._resolve(str(args["name"]))
        result = self.apps.launch(app)
        log.info("application launched", app=app.name, pid=result.pid, source=app.source)
        return ToolResult(
            ok=True,
            summary=f"Started {app.name}",
            data={
                "app": app.to_dict(),
                "pid": result.pid,
                "detail": result.detail,
                # Recorded so the Activity log can show which rung of the
                # control ladder was used (ARCHITECTURE section 10).
                "controlLayer": "L1",
            },
            changes=[f"Started {app.name}"],
        )

    async def observe(self, args: dict[str, Any]) -> dict[str, Any]:
        """Count matching processes, so a launch can be verified rather than assumed."""
        if str(args.get("operation")) != "launch" or not args.get("name"):
            return {}
        try:
            app = self._resolve(str(args["name"]))
        except ToolError:
            return {}
        return {"running": len(self.processes.find(app.key))}

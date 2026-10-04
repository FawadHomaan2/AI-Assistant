"""WindowTool — focus, minimise, maximise and close application windows.

This is the one part of Phase 4 with no cross-platform equivalent. On anything
but Windows every operation raises `PlatformUnsupported` with an explanation,
rather than returning an empty list that would read as "you have no windows
open".

Closing is deliberately a *request*: it posts the same WM_CLOSE the window's X
button sends, so the application can prompt about unsaved work. Force-ending a
program is a separate, higher-risk operation on the process tool.
"""

from __future__ import annotations

from typing import Any

from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.platform_.base import WindowBackend
from jarvis.tools.base import Preview, Tool, ToolError, ToolInputInvalid, ToolResult, ToolSpec
from jarvis.util.errors import PlatformUnsupported
from jarvis.util.logging import get_logger

log = get_logger(__name__)

OPERATIONS = ("list", "focus", "minimise", "maximise", "restore", "close", "foreground")

_RISK: dict[str, Risk] = {
    "list": Risk.SAFE,
    "foreground": Risk.SAFE,
    "focus": Risk.LOW,
    "minimise": Risk.LOW,
    "maximise": Risk.LOW,
    "restore": Risk.LOW,
    # Closing can lose unsaved work, so it asks.
    "close": Risk.MEDIUM,
}


class WindowTool(Tool):
    def __init__(self, backend: WindowBackend) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        available = True
        reason = ""
        try:
            self.backend.list_all()
        except PlatformUnsupported as exc:
            available, reason = False, exc.message
        except Exception as exc:
            # A runtime failure is not the same as the feature being absent, so
            # the tool stays available — but the reason is worth recording.
            log.debug("window probe failed", error=str(exc))
        return ToolSpec(
            name="window",
            description=(
                "See which application windows are open, and focus, minimise, "
                "maximise or close one. Closing asks the program to close, so it "
                "can prompt you about unsaved work."
            ),
            scopes=[Scope.WINDOW_MANAGE],
            risk=Risk.SAFE,
            input_schema={
                "type": "object",
                "required": ["operation"],
                "properties": {
                    "operation": {"type": "string", "enum": list(OPERATIONS)},
                    "handle": {"type": "integer", "description": "Window handle from `list`"},
                    "title": {
                        "type": "string",
                        "description": "Match a window by its title instead of a handle",
                    },
                },
            },
            available=available,
            unavailable_reason=reason,
        )

    def risk_for(self, args: dict[str, Any], preview: Preview) -> Risk:
        del preview
        return _RISK.get(str(args.get("operation", "")), Risk.MEDIUM)

    def scopes_for(self, args: dict[str, Any]) -> list[Scope]:
        operation = str(args.get("operation", ""))
        if operation in ("list", "foreground"):
            return [Scope.PROCESS_READ]
        if operation == "close":
            return [Scope.WINDOW_MANAGE, Scope.APP_CONTROL]
        return [Scope.WINDOW_MANAGE]

    def _find(self, args: dict[str, Any]) -> Any:
        """Resolve a window by handle, or by a distinctive part of its title."""
        windows = self.backend.list_all()
        if handle := args.get("handle"):
            found = next((w for w in windows if w.handle == int(handle)), None)
            if found is None:
                raise ToolError(f"No open window with handle {handle}. It may have been closed.")
            return found

        title = str(args.get("title") or "").strip().lower()
        if not title:
            raise ToolInputInvalid("This needs a window handle or part of a window title.")

        matches = [w for w in windows if title in w.title.lower()]
        if not matches:
            matches = [w for w in windows if title in w.process_name.lower()]
        if not matches:
            raise ToolError(f"No open window matches {args.get('title')!r}.")
        if len(matches) > 1:
            listed = "; ".join(f"{w.title} ({w.process_name})" for w in matches[:6])
            raise ToolError(
                f"{args.get('title')!r} matches {len(matches)} open windows: {listed}. "
                f"Which one did you mean?"
            )
        return matches[0]

    async def preview(self, args: dict[str, Any]) -> Preview:
        operation = str(args.get("operation", ""))
        if operation not in OPERATIONS:
            raise ToolInputInvalid(
                f"Unknown operation {operation!r}. Supported: {', '.join(OPERATIONS)}."
            )

        if operation in ("list", "foreground"):
            return Preview(
                summary="List open windows — inspection only",
                affected=0,
                reversible="undoable",
                blast_radius="Nothing changes.",
            )

        try:
            window = self._find(args)
        except PlatformUnsupported:
            raise
        except ToolError as exc:
            return Preview(
                summary=f"Cannot {operation} that window",
                blocked=exc.message,
                reversible="undoable",
                blast_radius="Nothing changes.",
            )

        if operation == "close":
            return Preview(
                summary=f"Close {window.title}",
                targets=[f"{window.title} ({window.process_name})"],
                affected=1,
                # The application decides; it may refuse or prompt to save.
                reversible="unknown",
                blast_radius=(
                    f"Asks {window.process_name or 'the program'} to close its window. "
                    f"If there is unsaved work it should prompt you — but if it does "
                    f"not, that work is lost."
                ),
            )

        return Preview(
            summary=f"{operation.capitalize()} {window.title}",
            targets=[window.title],
            affected=1,
            reversible="undoable",
            blast_radius=f"Changes how {window.title} is displayed. Nothing is lost.",
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        operation = str(args.get("operation", ""))

        if operation == "list":
            windows = self.backend.list_all()
            return ToolResult(
                ok=True,
                summary=f"{len(windows)} open window{'s' if len(windows) != 1 else ''}",
                data={"windows": [w.to_dict() for w in windows], "controlLayer": "L1"},
            )

        if operation == "foreground":
            window = self.backend.foreground()
            return ToolResult(
                ok=True,
                summary=(
                    f"Foreground window: {window.title}" if window else "No window is in focus"
                ),
                data={"window": window.to_dict() if window else None},
            )

        window = self._find(args)
        action = {
            "focus": self.backend.focus,
            "minimise": self.backend.minimise,
            "maximise": self.backend.maximise,
            "restore": self.backend.restore,
            "close": self.backend.close,
        }.get(operation)
        if action is None:
            raise ToolInputInvalid(f"Unsupported operation {operation!r}.")

        ok = action(window.handle)
        verb = {
            "focus": "Brought to the front",
            "minimise": "Minimised",
            "maximise": "Maximised",
            "restore": "Restored",
            "close": "Asked to close",
        }[operation]

        log.info("window operation", operation=operation, title=window.title, ok=ok)
        return ToolResult(
            ok=ok,
            summary=f"{verb}: {window.title}",
            data={"window": window.to_dict(), "controlLayer": "L1"},
            changes=[f"{verb} {window.title}"],
            error="" if ok else f"Windows refused to {operation} that window.",
        )

    async def observe(self, args: dict[str, Any]) -> dict[str, Any]:
        """Window state before and after, so the result can be verified."""
        operation = str(args.get("operation", ""))
        if operation in ("list", "foreground"):
            return {}
        try:
            window = self._find(args)
        except Exception:
            return {"present": False}
        return {
            "present": True,
            "minimised": window.minimised,
            "maximised": window.maximised,
            "foreground": window.foreground,
        }

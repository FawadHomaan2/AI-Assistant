"""ScreenshotTool, ClipboardTool, NotificationTool.

Screenshots and the clipboard are the two tools in this project that read what
you are doing rather than what you asked about, so both are treated carefully:
a screenshot is only ever taken on an explicit request, it is written to disk
where you can see it, and the interface shows an indicator while capture is
active. There is no scheduled, background or periodic capture anywhere in this
codebase, and there will not be — that is surveillance, not assistance.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.config import paths
from jarvis.governance.pathjail import Access, PathJail
from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.tools.base import Preview, Tool, ToolError, ToolInputInvalid, ToolResult, ToolSpec
from jarvis.util.errors import PlatformUnsupported
from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: Clipboard content above this is almost certainly not something to hand a
#: model, and may be a pasted file.
MAX_CLIPBOARD_CHARS = 100_000


def _screenshot_backend() -> tuple[Any, str]:
    """Return (module, detail). Raises if no capture library is usable."""
    try:
        import mss

        return mss, "mss"
    except ImportError as exc:
        raise PlatformUnsupported(
            "Taking screenshots needs the 'mss' package, which is not installed. "
            "Install the 'capture' extra to enable it."
        ) from exc


class ScreenshotTool(Tool):
    def __init__(self, jail: PathJail) -> None:
        self.jail = jail

    @property
    def spec(self) -> ToolSpec:
        available, reason = True, ""
        try:
            _screenshot_backend()
        except PlatformUnsupported as exc:
            available, reason = False, exc.message
        return ToolSpec(
            name="screenshot",
            description=(
                "Capture the screen to an image file. Only on explicit request — "
                "Jarvis never captures the screen in the background."
            ),
            scopes=[Scope.SCREEN_CAPTURE],
            risk=Risk.MEDIUM,
            input_schema={
                "type": "object",
                "properties": {
                    "monitor": {"type": "integer", "default": 0, "description": "0 = all screens"},
                    "path": {
                        "type": "string",
                        "description": "Where to save; defaults to the Jarvis data folder",
                    },
                },
            },
            available=available,
            unavailable_reason=reason,
        )

    def _default_path(self) -> Path:
        stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
        folder = paths.data_dir() / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"screenshot-{stamp}.png"

    async def preview(self, args: dict[str, Any]) -> Preview:
        try:
            _screenshot_backend()
        except PlatformUnsupported as exc:
            return Preview(
                summary="Screenshots are unavailable",
                blocked=exc.message,
                reversible="undoable",
                blast_radius="Nothing changes.",
            )
        target = (
            self.jail.check(str(args["path"]), Access.WRITE).path
            if args.get("path")
            else self._default_path()
        )
        return Preview(
            summary=f"Capture the screen to {target.name}",
            targets=[str(target)],
            affected=1,
            reversible="undoable",
            blast_radius=(
                "Writes one image of whatever is currently on screen. Anything "
                "visible — open documents, messages, passwords in plain sight — "
                "will be in that file."
            ),
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        mss_module, detail = _screenshot_backend()
        target = (
            self.jail.check(str(args["path"]), Access.WRITE).path
            if args.get("path")
            else self._default_path()
        )
        monitor_index = int(args.get("monitor") or 0)

        try:
            with mss_module.mss() as sct:
                monitors = sct.monitors
                if monitor_index >= len(monitors):
                    raise ToolError(
                        f"There is no monitor {monitor_index}; this computer has "
                        f"{len(monitors) - 1}."
                    )
                shot = sct.grab(monitors[monitor_index])
                mss_module.tools.to_png(shot.rgb, shot.size, output=str(target))
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Could not capture the screen: {exc}") from exc

        log.info("screenshot captured", path=str(target), monitor=monitor_index)
        return ToolResult(
            ok=True,
            summary=f"Saved a screenshot to {target.name}",
            data={
                "path": str(target),
                "bytes": target.stat().st_size if target.exists() else 0,
                "backend": detail,
            },
            changes=[f"Wrote {target}"],
        )

    async def observe(self, args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("path"):
            return {}
        try:
            resolved = self.jail.check(str(args["path"]), Access.WRITE).path
        except Exception:
            return {}
        return {"exists": resolved.exists()}


def _clipboard_text() -> str:
    """Read the clipboard, or explain why it cannot be read."""
    if sys.platform == "win32":
        try:
            import ctypes

            cf_unicodetext = 13
            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            if not user32.OpenClipboard(0):
                raise ToolError("Another program is holding the clipboard open.")
            try:
                handle = user32.GetClipboardData(cf_unicodetext)
                if not handle:
                    return ""
                pointer = kernel32.GlobalLock(handle)
                try:
                    return ctypes.c_wchar_p(pointer).value or ""
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        except AttributeError as exc:
            raise PlatformUnsupported("The clipboard API is unavailable.") from exc

    # Development fallback.
    import shutil

    for program in ("wl-paste", "xclip", "pbpaste"):
        if path := shutil.which(program):
            from jarvis.util.subprocess import run_command

            argv = [path, "-selection", "clipboard", "-o"] if "xclip" in program else [path]
            result = run_command(argv, timeout=5)
            if result.exit_code == 0:
                return result.stdout
    raise PlatformUnsupported(
        "No clipboard tool is available on this platform. On Linux install "
        "xclip or wl-clipboard; on Windows this uses the native API."
    )


class ClipboardTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="clipboard",
            description="Read what is on the clipboard, or put text on it.",
            scopes=[Scope.FS_READ],
            risk=Risk.SAFE,
            input_schema={
                "type": "object",
                "required": ["operation"],
                "properties": {
                    "operation": {"type": "string", "enum": ["read", "write"]},
                    "text": {"type": "string"},
                },
            },
        )

    def risk_for(self, args: dict[str, Any], preview: Preview) -> Risk:
        del preview
        # Writing replaces whatever the user had copied, which is mildly
        # destructive; reading is not.
        return Risk.LOW if str(args.get("operation")) == "write" else Risk.SAFE

    async def preview(self, args: dict[str, Any]) -> Preview:
        operation = str(args.get("operation", "read"))
        if operation not in ("read", "write"):
            raise ToolInputInvalid("Operation must be 'read' or 'write'.")
        if operation == "read":
            return Preview(
                summary="Read the clipboard",
                affected=0,
                reversible="undoable",
                blast_radius=(
                    "Reads whatever you last copied. If that was a password, it will be read."
                ),
            )
        return Preview(
            summary="Replace the clipboard contents",
            affected=1,
            reversible="permanent",
            blast_radius="Whatever you had copied is replaced and cannot be recovered.",
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        operation = str(args.get("operation", "read"))
        if operation == "read":
            text = _clipboard_text()
            truncated = len(text) > MAX_CLIPBOARD_CHARS
            return ToolResult(
                ok=True,
                summary=(
                    f"Clipboard holds {len(text)} characters" if text else "The clipboard is empty"
                ),
                data={
                    "text": text[:MAX_CLIPBOARD_CHARS],
                    "characters": len(text),
                    "truncated": truncated,
                },
            )

        self.require(args, "text")
        text = str(args["text"])
        if sys.platform == "win32":
            raise ToolError("Writing to the clipboard on Windows is not implemented yet.")
        import shutil

        for program, argv in (
            ("wl-copy", ["wl-copy"]),
            ("xclip", ["xclip", "-selection", "clipboard"]),
        ):
            if shutil.which(program):
                import subprocess

                subprocess.run(argv, input=text, text=True, check=False, timeout=5)  # noqa: S603
                return ToolResult(
                    ok=True,
                    summary=f"Copied {len(text)} characters to the clipboard",
                    data={"characters": len(text)},
                    changes=["Replaced the clipboard contents"],
                )
        raise PlatformUnsupported("No clipboard tool is available on this platform.")


class NotificationTool(Tool):
    """Desktop notifications. The one tool whose whole job is being noticed."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="notify",
            description="Show a desktop notification.",
            scopes=[Scope.SYSTEM_INFO],
            risk=Risk.LOW,
            input_schema={
                "type": "object",
                "required": ["title", "message"],
                "properties": {
                    "title": {"type": "string", "maxLength": 120},
                    "message": {"type": "string", "maxLength": 500},
                },
            },
        )

    async def preview(self, args: dict[str, Any]) -> Preview:
        self.require(args, "title", "message")
        return Preview(
            summary=f"Show a notification: {args['title']}",
            affected=1,
            reversible="undoable",
            blast_radius="Shows a message on screen. Nothing else changes.",
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        self.require(args, "title", "message")
        title, message = str(args["title"]), str(args["message"])

        # The desktop shell owns notifications: it already has the Tauri plugin
        # and the right to show them. The core returns the payload for it to
        # display rather than duplicating platform code here.
        log.info("notification requested", title=title)
        return ToolResult(
            ok=True,
            summary=f"Notification: {title}",
            data={"title": title, "message": message, "displayedBy": "desktop-shell"},
            changes=[f"Notified: {title}"],
        )

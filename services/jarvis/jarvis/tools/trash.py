"""Recycle Bin / Trash.

Deleting means "send to the Recycle Bin" unless the user explicitly asks for a
permanent delete, because a recoverable mistake is a very different event from
an unrecoverable one.

Windows uses `IFileOperation` through the shell, which puts the file in the real
Recycle Bin with working Undo in Explorer. On Linux/macOS (development) it
follows the freedesktop trash spec well enough to be reversible. If no mechanism
is available the call fails rather than silently deleting permanently.
"""

from __future__ import annotations

import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from jarvis.util.errors import JarvisError, PlatformUnsupported
from jarvis.util.logging import get_logger

log = get_logger(__name__)


def _platform() -> str:
    """Read through a function so every branch below stays type-checked.

    Referencing `sys.platform` directly lets mypy narrow it to the host and call
    the other platforms' branches unreachable, which hides real errors in them.
    """
    return sys.platform


class TrashUnavailable(JarvisError):
    code = "jarvis.trash.unavailable"
    http_status = 501


@dataclass
class TrashResult:
    path: Path
    method: str
    recoverable: bool


def _windows_recycle(path: Path) -> TrashResult:
    """Send to the Recycle Bin via the Windows shell."""
    try:
        import pythoncom
        from win32com.shell import shell, shellcon
    except ImportError as exc:
        raise TrashUnavailable(
            "The Windows shell bindings (pywin32) are not installed, so Jarvis "
            "cannot use the Recycle Bin. It will not delete permanently instead."
        ) from exc

    pythoncom.CoInitialize()
    try:
        operation = pythoncom.CoCreateInstance(
            shell.CLSID_FileOperation,
            None,
            pythoncom.CLSCTX_ALL,
            shell.IID_IFileOperation,
        )
        # ALLOWUNDO is what puts it in the bin rather than unlinking it.
        operation.SetOperationFlags(
            shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT
        )
        item = shell.SHCreateItemFromParsingName(str(path), None, shell.IID_IShellItem)
        operation.DeleteItem(item)
        operation.PerformOperations()
        return TrashResult(path, "windows-recycle-bin", recoverable=True)
    finally:
        pythoncom.CoUninitialize()


def _freedesktop_trash(path: Path) -> TrashResult:
    """Move into ~/.local/share/Trash with the metadata that makes restore work."""
    home = Path.home()
    trash = home / ".local" / "share" / "Trash"
    files_dir, info_dir = trash / "files", trash / "info"
    files_dir.mkdir(parents=True, exist_ok=True)
    info_dir.mkdir(parents=True, exist_ok=True)

    target = files_dir / path.name
    suffix = 1
    while target.exists():
        target = files_dir / f"{path.stem}.{suffix}{path.suffix}"
        suffix += 1

    info = info_dir / f"{target.name}.trashinfo"
    info.write_text(
        "[Trash Info]\n"
        f"Path={quote(str(path.resolve()))}\n"
        f"DeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n",
        encoding="utf-8",
    )
    shutil.move(str(path), str(target))
    return TrashResult(target, "freedesktop-trash", recoverable=True)


def send_to_trash(path: Path) -> TrashResult:
    """Move a file or folder to the platform's recoverable trash."""
    if not path.exists():
        raise JarvisError(f"{path} does not exist, so there is nothing to delete.")

    platform = _platform()
    if platform == "win32":
        return _windows_recycle(path)
    if platform in ("linux", "darwin"):
        return _freedesktop_trash(path)
    raise PlatformUnsupported(
        f"Jarvis does not know how to use a recoverable trash on {platform}, "
        f"so it will not delete this file."
    )


def trash_available() -> tuple[bool, str]:
    """Whether a recoverable delete is possible, and why not if it is not."""
    platform = _platform()
    if platform == "win32":
        try:
            import win32com.shell  # noqa: F401

            return True, "Windows Recycle Bin"
        except ImportError:
            return False, "pywin32 is not installed, so the Recycle Bin is unavailable"
    if platform in ("linux", "darwin"):
        return True, "freedesktop trash"
    return False, f"no recoverable trash on {platform}"

"""Well-known user folders.

The model produces paths like "my Desktop" or "Downloads". Resolving those
through a fixed table is much safer than expanding arbitrary environment
variables out of model output, which would be an injection vector: a crafted
string like "%WINDIR%\\System32" must never become a usable path just because it
looked like a folder name.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

#: Tokens the agent may use, mapped to a resolver. Nothing else expands.
KNOWN_FOLDERS = (
    "desktop",
    "documents",
    "downloads",
    "pictures",
    "music",
    "videos",
    "home",
)


def _windows_known_folder(name: str) -> Path | None:
    """Ask Windows where a known folder actually is.

    Hard-coding `~/Desktop` is wrong on machines where the folder is redirected
    to OneDrive or a network share, which is common.
    """
    try:
        import ctypes
        import ctypes.wintypes

        guids = {
            "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
            "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
            "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
            "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
            "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
            "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
            "home": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
        }
        guid = guids.get(name)
        if guid is None:
            return None

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        clsid = GUID()
        ctypes.windll.ole32.CLSIDFromString(guid, ctypes.byref(clsid))  # type: ignore[attr-defined]
        buf = ctypes.c_wchar_p()
        if (
            ctypes.windll.shell32.SHGetKnownFolderPath(  # type: ignore[attr-defined]
                ctypes.byref(clsid), 0, None, ctypes.byref(buf)
            )
            != 0
        ):
            return None
        try:
            return Path(buf.value) if buf.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(buf)  # type: ignore[attr-defined]
    except Exception:
        return None


@lru_cache(maxsize=1)
def folders() -> dict[str, Path]:
    """Resolve every known folder for this user."""
    home = Path(os.environ.get("JARVIS_HOME_OVERRIDE") or Path.home())
    resolved: dict[str, Path] = {"home": home}

    for name in KNOWN_FOLDERS:
        if name == "home":
            continue
        path: Path | None = None
        if sys.platform == "win32" and not os.environ.get("JARVIS_HOME_OVERRIDE"):
            path = _windows_known_folder(name)
        resolved[name] = path or (home / name.capitalize())

    return resolved


def resolve_token(token: str) -> Path | None:
    """Map a known-folder name to a path, or None if it is not one."""
    return folders().get(token.strip().lower().lstrip("~").strip("<>"))


def default_allowed_roots() -> list[Path]:
    """Folders granted on a fresh install.

    Deliberately the user's own document folders and nothing else: not the whole
    home directory, which holds credentials, SSH keys, browser profiles and
    application data.
    """
    known = folders()
    return [known[n] for n in ("desktop", "documents", "downloads", "pictures") if n in known]


def reset_cache() -> None:
    folders.cache_clear()

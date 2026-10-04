"""Which optional features this build actually has.

Every heavy capability here is an optional dependency, and each one reports
itself unavailable with a reason when its package is missing. That is the right
behaviour at runtime and a trap at build time: PyInstaller finds imports by
scanning source, and several of these are loaded by entry point or inside the
function that needs them, so a bundle can be missing one and still build, start
and pass a smoke test. The feature is simply absent, and the first person to
find out is a user.

`keyring` did exactly that — the installer shipped without it for a release, so
no API key could be stored at all. So the build asks the frozen binary what it
has rather than trusting the spec file, which is the thing that gets this wrong.

Import, not `find_spec`: a native extension whose shared library did not make
it in resolves fine and fails on import, and import is what the code does.
"""

from __future__ import annotations

import importlib
from typing import Any

#: Package name -> what stops working without it. Written for someone reading a
#: build log or a bug report, not as a dependency list.
OPTIONAL_PACKAGES: dict[str, str] = {
    "keyring": "storing API keys in the OS credential store",
    "onnxruntime": "running the wake word and the Piper voice",
    "openwakeword": "the 'Hey Jarvis' wake word",
    "faster_whisper": "speech to text",
    "piper": "the spoken reply",
    "sounddevice": "microphone capture",
    "webrtcvad": "noticing when you stop speaking",
    "playwright": "web browsing",
    "pypdf": "reading PDFs",
    "docx": "reading Word documents",
    "openpyxl": "reading Excel workbooks",
    "pptx": "reading PowerPoint files",
    "PIL": "screenshots and image handling",
}


def available(package: str) -> tuple[bool, str]:
    """Whether `package` imports here, and the error if it does not."""
    try:
        importlib.import_module(package)
    except Exception as exc:
        # Not only ImportError: a native extension missing its shared library
        # raises OSError, and that is the failure a frozen bundle produces.
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def report() -> dict[str, Any]:
    """What this build can do, as a structure fit for a JSON line."""
    packages: dict[str, Any] = {}
    for package, purpose in OPTIONAL_PACKAGES.items():
        ok, detail = available(package)
        packages[package] = {"available": ok, "purpose": purpose, "detail": detail}
    return {
        "packages": packages,
        "present": sorted(name for name, row in packages.items() if row["available"]),
        "missing": sorted(name for name, row in packages.items() if not row["available"]),
    }

# PyInstaller spec for the Jarvis core sidecar.
#
# Produces a single `jarvis-core` executable that the Tauri shell spawns and
# supervises. The shell looks for it beside the app and falls back to running
# the Python package from a repository checkout, so `tauri dev` works without
# building this first.
#
# Deliberate choices:
#
#   **One file, not one folder.** A folder build starts faster, but the shell
#   has to locate an interpreter inside it, and an installer that lays down
#   2,000 files is one an antivirus scans for a minute on first run. One
#   executable is also one thing to sign.
#
#   **Console enabled.** The handshake is a JSON line on stdout and the shell
#   reads it; a windowed build on Windows has no stdout at all, which looks
#   exactly like a core that failed to start. The shell spawns it with
#   CREATE_NO_WINDOW so no console is ever visible.
#
#   **Optional dependencies are excluded rather than bundled.** Whisper, Piper,
#   ONNX, Playwright and the document readers are large, and every one of them
#   is a capability that reports itself unavailable with a reason when missing.
#   Bundling them would quadruple the installer for features many users never
#   turn on. They are installed on demand; see docs/PACKAGING.md.
#
# Build:  pyinstaller --clean --noconfirm jarvis-core.spec

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

HERE = Path(SPECPATH).resolve()

# Everything under `jarvis.` — the tools and platform adapters are resolved
# dynamically, so a static import scan misses several of them.
hidden = collect_submodules("jarvis")

# uvicorn and pydantic load pieces by name at runtime.
hidden += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "pydantic.deprecated.decorator",
]

if sys.platform == "win32":
    # The Windows adapters import these lazily, inside the functions that need
    # them, so the scan does not see them at all.
    hidden += ["win32api", "win32con", "win32gui", "win32process", "winreg"]

# Heavy optional extras. Each has a runtime check that explains what is missing
# and what to install, so excluding them degrades a feature rather than
# crashing the core.
excluded = [
    "onnxruntime",
    "openwakeword",
    "faster_whisper",
    "piper",
    "playwright",
    "torch",
    "numpy.distutils",
    "tkinter",
    "test",
    "unittest",
    "pydoc_data",
]

a = Analysis(
    [str(HERE / "jarvis" / "__main__.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excluded,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="jarvis-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX makes the binary smaller and makes antivirus heuristics much less
    # happy about it. An unsigned assistant that spawns PowerShell already has
    # enough to explain to SmartScreen.
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

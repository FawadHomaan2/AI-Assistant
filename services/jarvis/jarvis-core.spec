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
#   **Voice is bundled; the browser is not.** Voice was excluded to keep the
#   installer small, which meant "Hey Jarvis" did not work from the thing
#   people download — the wake word, speech-to-text and the spoken reply all
#   reported themselves missing, and the only fix was a source checkout. That
#   is the feature least worth asking someone to clone a repository for, so
#   onnxruntime, openwakeword, faster-whisper, Piper and the audio layer are in.
#   The models they load are not: those are downloaded on demand and verified
#   by checksum, which is why the installer is far smaller than the sum of its
#   capabilities.
#
#   Playwright stays out. It needs a browser engine, not just a package, and
#   `playwright install` is a download either way — so bundling the Python half
#   alone would add weight without making the feature work.
#
#   Each bundled package is collected whole with `collect_all`, rather than by
#   listing hidden imports: every one of them loads something by entry point or
#   ships a native library, and a guessed list that is one entry short produces
#   a bundle that builds, starts, passes a smoke test, and quietly lacks the
#   feature. `scripts/build_core.py` asks the frozen binary which packages it
#   actually has, so the list below is checked rather than trusted.
#
# Build:  pyinstaller --clean --noconfirm jarvis-core.spec

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

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

# `keyring` finds its backends through entry points, so nothing imports
# `keyring.backends.Windows` by name and a static scan bundles the package
# without the one backend that works. The result is an installed app whose
# credential store reports itself unavailable — which is where API keys for
# Claude, ChatGPT and Gemini have nowhere to go. `scripts/build_core.py` asks
# the frozen binary about this rather than trusting the list below.
hidden += [
    "keyring.backends.fail",
    "keyring.backends.null",
    "keyring.backends.chainer",
]
if sys.platform == "win32":
    hidden += [
        "keyring.backends.Windows",
        "win32ctypes.core",
        "win32ctypes.pywin32.win32cred",
    ]

# The voice stack, collected whole: submodules, data files and native
# libraries. `espeakng_loader` is Piper's phonemiser and carries both a shared
# library and its language data; `ctranslate2` is what faster-whisper actually
# runs the model on. Neither is imported by name anywhere in this codebase.
datas: list = []
binaries: list = []
#
# The document readers are here for a different reason: `jarvis.tools.documents`
# loads them with `__import__(module)` where `module` is a variable, which a
# static scan cannot follow. They were installed for every build and bundled by
# none of them, so the shipped installer reported "the 'pypdf' package is not
# installed" for a dependency that was. They also ship data files — python-docx
# and python-pptx carry the default templates they open.
for package in (
    "onnxruntime",
    "openwakeword",
    "faster_whisper",
    "ctranslate2",
    "piper",
    "espeakng_loader",
    "sounddevice",
    "webrtcvad",
    "pypdf",
    "docx",
    "openpyxl",
    "pptx",
    "PIL",
):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception as exc:  # noqa: BLE001 - a missing optional package is fine
        print(f"[spec] {package} is not installed, so it will not be bundled ({exc})")
        continue
    datas += package_datas
    binaries += package_binaries
    hidden += package_hidden

def _destination(entry) -> str:
    """Where an entry lands inside the bundle.

    `collect_all` yields `(absolute_source_path, destination_directory)` — in
    that order, which is the opposite of the `(name, path, kind)` triples a
    PyInstaller TOC prints. Filtering on `entry[0]` therefore matches an
    absolute path into site-packages and silently does nothing, which is how the
    first attempt at the trimming below achieved precisely zero bytes.
    """
    source, dest_dir = entry[0], entry[1]
    folder = str(dest_dir).replace("\\", "/").strip("/")
    name = Path(str(source)).name
    return f"{folder}/{name}" if folder and folder != "." else name


# Piper ships a neural diacritiser per script that needs one, and they are not
# equally droppable — `piper/voice.py` imports Hebrew's inside the branch that
# uses it (line 227) and Arabic's at module level (line 21). So Nakdimon, 21 MB
# for a script this build's voice cannot speak, is filtered out; Tashkeel stays,
# because without it `import piper` fails outright and there is no spoken reply
# at all. Dropping it is what the first attempt did, and the build caught it:
# "piper — ModuleNotFoundError: No module named 'piper.tashkeel'".
#
# `espeak-ng-data`, which stays, is what phonemises everything else. A Hebrew
# Piper voice needs a source install. `piper/train` is the training code; it
# imports torch, which is excluded, so collecting it only produces warnings.
UNUSED_PIPER = ("piper/hebrew/", "piper/train/")
datas = [item for item in datas if not _destination(item).startswith(UNUSED_PIPER)]
hidden = [
    name
    for name in hidden
    if not name.startswith(("piper.hebrew", "piper.phonemize_hebrew", "piper.train"))
]

# `collect_all` returns some files as both a data file and a binary —
# `libonnxruntime.so` is 28 MB listed twice — and PyInstaller packs each entry,
# so a duplicate is paid for twice in the download. Deduplicated by where the
# file lands, keeping the binary entry, which is the one that gets the loader
# fixups applied to it.


def _unique(items: list, taken: set | None = None) -> list:
    seen: set = set() if taken is None else set(taken)
    out = []
    for item in items:
        key = _destination(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


binaries = _unique(binaries)
datas = _unique(datas, taken={_destination(item) for item in binaries})

# Still excluded. Playwright needs a browser engine rather than just a package,
# so bundling it would add weight without making web browsing work; torch is
# not used at all (faster-whisper runs on ctranslate2); the rest is stdlib
# ballast.
# `unittest` is NOT excluded, though it looks like obvious ballast: openwakeword
# imports scipy, whose array-API shim imports numpy, which imports
# numpy.testing, which imports unittest — at import time, not in a test. With it
# excluded the bundle built and started, and the wake word reported
# "ModuleNotFoundError: No module named 'unittest'".
excluded = [
    "playwright",
    "torch",
    "piper.hebrew",
    "piper.phonemize_hebrew",
    "piper.train",
    "numpy.distutils",
    "tkinter",
    "test",
    "pydoc_data",
]

a = Analysis(
    [str(HERE / "jarvis" / "__main__.py")],
    pathex=[str(HERE)],
    binaries=binaries,
    datas=datas,
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

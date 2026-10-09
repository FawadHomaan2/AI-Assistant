#!/usr/bin/env python3
"""Build the Jarvis core into a single executable the desktop shell can spawn.

Runs on whatever platform you run it on: PyInstaller does not cross-compile, so
the Windows executable must be built on Windows. This script is the same on
both, and `scripts/build_windows.ps1` calls it.

It does three things beyond invoking PyInstaller, each because skipping them
produces an installer that is broken in a way nobody notices until a user
reports it:

  1. **Checks the frozen binary actually starts** and prints its handshake.
     A PyInstaller build succeeds happily with a missing hidden import; the
     failure appears the first time a user launches it.
  2. **Copies it where Tauri expects.** The core is a one-folder PyInstaller
     bundle, so it ships through `resources` rather than `externalBin` — the
     latter carries a single renamed executable, and this is a tree whose
     executable finds `_internal` beside itself. Getting the layout wrong
     produces an installer that builds and then cannot start its own core.
  3. **Reports the size**, so a dependency that quietly adds 300 MB is noticed
     in the build log rather than in the download.
"""

# Every subprocess call here passes a fixed argument list and never a shell
# string: rustc, this interpreter, and the binary this script just produced.
# ruff: noqa: S603, S607

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "services" / "jarvis"
SIDECAR_DIR = ROOT / "apps" / "desktop" / "src-tauri" / "binaries"
NAME = "jarvis-core"

#: How long the frozen binary gets to print its handshake before we call it broken.
STARTUP_TIMEOUT = 45.0


def build() -> Path:
    print(f"[core] building with PyInstaller in {CORE}")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            "dist-core",
            "--workpath",
            "build-core",
            "jarvis-core.spec",
        ],
        cwd=CORE,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit("[core] PyInstaller failed")

    # A folder now, not a single file: see the comment on EXE in the spec.
    # The executable only runs from inside it, with `_internal` beside it.
    folder = CORE / "dist-core" / NAME
    produced = folder / (f"{NAME}.exe" if os.name == "nt" else NAME)
    if not produced.exists():
        raise SystemExit(
            f"[core] expected {produced} and it is not there. "
            "A one-file spec would have put the executable one level up, so "
            "check that EXE(exclude_binaries=True) and COLLECT still agree."
        )
    size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    print(f"[core] built {folder} ({size / 1_048_576:.0f} MB over {_count(folder)} files)")
    return produced


def _count(folder: Path) -> int:
    return sum(1 for f in folder.rglob("*") if f.is_file())


def smoke_test(binary: Path) -> None:
    """Start it and read the handshake. A build that cannot start is not a build."""
    print("[core] checking the binary starts and talks")
    scratch = CORE / "build-core" / "smoke"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "JARVIS_DATA_DIR": str(scratch),
        "JARVIS_CONFIG_DIR": str(scratch),
    }
    process = subprocess.Popen(
        [str(binary), "--db", str(scratch / "smoke.db")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        line = ""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = (process.stderr.read() if process.stderr else "")[-800:]
                raise SystemExit(f"[core] the binary exited at startup:\n{stderr}")
            if process.stdout is not None:
                line = process.stdout.readline()
                if line.strip():
                    break
        if not line.strip():
            raise SystemExit("[core] the binary never printed its handshake")
        payload = json.loads(line)
        if payload.get("jarvis") != "ready" or not payload.get("port"):
            raise SystemExit(f"[core] unexpected handshake: {line.strip()}")
        print(f"[core] handshake ok — version {payload.get('version')}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(scratch, ignore_errors=True)


def _excluded_by_the_spec() -> set[str]:
    """Top-level packages the spec's `excluded` list keeps out of the bundle.

    Read from the spec rather than repeated here, so there is one list. Only
    top-level names matter: `piper.hebrew` being excluded does not mean
    `piper` is, and the check is about whole packages.

    Parsed as source rather than imported, because a spec file is executed by
    PyInstaller with globals it provides, so importing it here would fail.
    """
    spec = (CORE / "jarvis-core.spec").read_text(encoding="utf-8")
    match = re.search(r"^excluded\s*=\s*\[(.*?)^\]", spec, re.S | re.M)
    if not match:
        # Not fatal: the worst case is the old behaviour, a build failed over a
        # package that was never meant to be in it.
        print("[core] warning: could not find `excluded` in the spec")
        return set()
    names = re.findall(r'"([^"]+)"', match.group(1))
    return {name for name in names if "." not in name}


def check_bundled_features(binary: Path) -> None:
    """Fail the build if a package present here did not make it into the bundle.

    PyInstaller finds imports by scanning source. Several optional packages are
    loaded by entry point or inside the function that needs them, so a bundle
    can be missing one and still build, start and pass a smoke test — the
    feature is just absent, and the first to find out is a user. The installer
    shipped without `keyring` for a release exactly this way, so no API key
    could be stored at all.

    The expected set is not written down anywhere: it is whatever imports in
    the environment doing the build. So installing an extra is enough to
    require it in the bundle, and dropping one does not leave a stale
    assertion behind.

    Minus what the spec deliberately leaves out. Playwright is excluded on
    purpose — it needs a browser engine, so bundling the Python half alone
    adds weight without making the feature work — and without reading that
    list, anyone whose build environment happens to have the browser extra
    installed got a failed build telling them to bundle a package the spec
    says in a comment not to bundle.
    """
    sys.path.insert(0, str(CORE))
    try:
        from jarvis.diagnostics import bundle
    finally:
        sys.path.pop(0)

    deliberate = _excluded_by_the_spec()
    expected = sorted(
        name
        for name in bundle.OPTIONAL_PACKAGES
        if name not in deliberate and bundle.available(name)[0]
    )
    if deliberate:
        print(f"[core] not bundled on purpose: {', '.join(sorted(deliberate))}")
    if not expected:
        print("[core] no optional packages installed here, so there is nothing to check")
        return

    result = subprocess.run(
        [str(binary), "--selfcheck"], capture_output=True, text=True, check=False, timeout=120
    )
    if result.returncode != 0:
        raise SystemExit(
            f"[core] `--selfcheck` failed with exit code {result.returncode}:\n"
            f"{result.stderr[-800:]}"
        )
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise SystemExit(f"[core] could not read the self-check output: {exc}") from exc

    packages = report.get("packages") or {}
    missing = [name for name in expected if not (packages.get(name) or {}).get("available")]
    if missing:
        lines = [
            "[core] these packages are installed for this build but did not make it",
            "[core] into the bundle, so the features they provide are silently absent:",
        ]
        for name in missing:
            row = packages.get(name) or {}
            lines.append(f"[core]   {name} — {row.get('purpose', '?')}")
            if row.get("detail"):
                lines.append(f"[core]       {row['detail']}")
        lines.append("[core] Add them to the collected packages in jarvis-core.spec.")
        raise SystemExit("\n".join(lines))

    print(f"[core] bundled optional features: {', '.join(expected)}")


def install(binary: Path) -> Path:
    """Copy the whole bundle folder to where Tauri bundles it as a resource.

    The core used to be one file and shipped as an `externalBin`, which exists
    to carry a single executable and renames it by target triple. A one-folder
    bundle is a tree, so it ships through `resources` instead and keeps its own
    name — the executable finds `_internal` by looking beside itself, so the
    layout has to survive intact.
    """
    destination = SIDECAR_DIR / NAME
    SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
    # Replaced wholesale rather than merged: a stale .pyd left behind by an
    # earlier build with different dependencies is loaded in preference to
    # nothing, and fails somewhere far from here.
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(binary.parent, destination)

    installed = destination / binary.name
    installed.chmod(0o755)
    print(f"[core] installed {_count(destination)} files to {destination}")
    return installed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-smoke-test",
        action="store_true",
        help="skip starting the binary (not recommended; this is what catches "
        "a missing hidden import)",
    )
    args = parser.parse_args()

    binary = build()
    if not args.skip_smoke_test:
        smoke_test(binary)
        check_bundled_features(binary)
    install(binary)
    print("[core] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

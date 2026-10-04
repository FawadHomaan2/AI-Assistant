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
  2. **Copies it where Tauri expects**, with the target triple suffix Tauri's
     `externalBin` requires. Getting that name wrong produces an installer that
     builds and then cannot find its own core.
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
import platform
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


def target_triple() -> str:
    """The suffix Tauri's `externalBin` appends, e.g. x86_64-pc-windows-msvc."""
    out = subprocess.run(
        ["rustc", "-vV"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in out.stdout.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    # Without rustc we can still guess the common cases rather than failing.
    machine = {"AMD64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64"}.get(
        platform.machine(), platform.machine()
    )
    system = {
        "Windows": "pc-windows-msvc",
        "Linux": "unknown-linux-gnu",
        "Darwin": "apple-darwin",
    }.get(platform.system(), "unknown")
    return f"{machine}-{system}"


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

    produced = CORE / "dist-core" / (f"{NAME}.exe" if os.name == "nt" else NAME)
    if not produced.exists():
        raise SystemExit(f"[core] expected {produced} and it is not there")
    print(f"[core] built {produced} ({produced.stat().st_size / 1_048_576:.0f} MB)")
    return produced


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


def install(binary: Path) -> Path:
    """Copy into place with the target-triple name Tauri's externalBin needs."""
    SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if binary.suffix == ".exe" else ""
    destination = SIDECAR_DIR / f"{NAME}-{target_triple()}{suffix}"
    shutil.copy2(binary, destination)
    destination.chmod(0o755)
    print(f"[core] installed {destination.name}")
    return destination


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
    install(binary)
    print("[core] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

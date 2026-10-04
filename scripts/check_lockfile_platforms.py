#!/usr/bin/env python3
"""Verify package-lock.json carries the native binaries Windows needs.

Run after any dependency change:  python3 scripts/check_lockfile_platforms.py

npm records only the optional dependencies it actually resolved. Regenerate the
lockfile on Linux with node_modules present and it keeps the Linux binaries and
silently drops every other platform's, because npm reads the installed tree
rather than asking the registry. The lockfile still installs perfectly here, so
nothing looks wrong until `npm ci` runs on Windows, installs the JS wrapper
without its native module, and `npm run tauri build` dies with
`Cannot find module './cli.win32-x64-msvc.node'` one second in.

That is what happened on the first Windows build (CI run 20). A Linux-only
lockfile cannot be caught by any Linux job that merely runs `npm ci`, so it is
checked here explicitly.

To fix a failure below, regenerate the lockfile with no tree to read from:

    cd apps/desktop
    mv node_modules /tmp/nm && rm -f package-lock.json
    npm install --package-lock-only
    rm -rf /tmp/nm && npm ci

Expect the package count to grow — the additions are other platforms' optional
binaries, which npm skips at install time on any host they do not match.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

LOCKFILE = Path(__file__).resolve().parent.parent / "apps/desktop/package-lock.json"

REMEDY = """Regenerate the lockfile with no installed tree for npm to read:

    cd apps/desktop
    mv node_modules /tmp/nm && rm -f package-lock.json
    npm install --package-lock-only
    rm -rf /tmp/nm && npm ci

Expect the package count to grow. The additions are other platforms' optional
binaries, which npm skips at install time on any host they do not match."""

# Each entry is a package the Windows x64 build loads as a native module, and
# what breaks without it. The Tauri CLI is the one that failed in CI; rollup and
# esbuild would have failed next, inside the frontend build Tauri invokes.
REQUIRED = {
    "@tauri-apps/cli-win32-x64-msvc": "npm run tauri build — the Tauri CLI itself",
    "@rollup/rollup-win32-x64-msvc": "npm run build — Vite's bundler",
    "@esbuild/win32-x64": "npm run build — Vite's transform step",
}

# The Linux binaries CI and this container install from. A regeneration that
# fixed Windows by breaking Linux would otherwise pass.
REQUIRED_LINUX = {
    "@tauri-apps/cli-linux-x64-gnu": "the Rust shell job and local development",
    "@rollup/rollup-linux-x64-gnu": "the frontend job",
    "@esbuild/linux-x64": "the frontend job",
}


def main() -> int:
    if not LOCKFILE.exists():
        print(f"No lockfile at {LOCKFILE}")
        return 1

    packages = json.loads(LOCKFILE.read_text())["packages"]
    present = {name.removeprefix("node_modules/") for name in packages if name}

    failures: list[str] = []
    for group, label in ((REQUIRED, "Windows x64"), (REQUIRED_LINUX, "Linux x64")):
        print(f"{label}:")
        for package, why in group.items():
            ok = package in present
            print(f"  {'ok  ' if ok else 'MISS'}  {package:38}  {why}")
            if not ok:
                failures.append(package)
        print()

    if failures:
        print("LOCKFILE IS MISSING NATIVE BINARIES:")
        for package in failures:
            print(f"  - {package}")
        print()
        print(REMEDY)
        return 1

    print(f"All required native binaries are in the lockfile ({len(present)} packages).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

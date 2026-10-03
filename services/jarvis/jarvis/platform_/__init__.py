"""OS adapters, selected once at startup.

`backends()` returns the right implementation for this machine. Everything above
it — the tools, the executor, the agents — is written once against the
interfaces in `base.py`.
"""

from __future__ import annotations

import sys
from functools import lru_cache

from jarvis.platform_.base import Backends
from jarvis.util.logging import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=1)
def backends() -> Backends:
    from jarvis.platform_.posix import (
        PosixAppBackend,
        PosixWindowBackend,
        PsutilProcessBackend,
    )

    processes = PsutilProcessBackend()  # psutil is the same on every platform

    if sys.platform == "win32":
        from jarvis.platform_.win32 import Win32AppBackend, Win32WindowBackend

        log.info("using Windows adapters")
        return Backends(processes, Win32WindowBackend(), Win32AppBackend(), "win32")

    log.info("using POSIX adapters (development); window management unavailable")
    return Backends(processes, PosixWindowBackend(), PosixAppBackend(), sys.platform)


def reset_cache() -> None:
    backends.cache_clear()


__all__ = ["Backends", "backends", "reset_cache"]

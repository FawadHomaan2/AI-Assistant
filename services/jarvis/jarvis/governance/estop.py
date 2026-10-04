"""Emergency stop.

The Rust shell owns the user-facing latch; this is the core's mirror of it.
Every long-running operation checks `engaged` between steps, and from Phase 3
every tool checks it before executing.
"""

from __future__ import annotations

import threading

from jarvis.util.logging import get_logger

log = get_logger(__name__)


class EmergencyStop:
    def __init__(self) -> None:
        self._flag = threading.Event()

    @property
    def engaged(self) -> bool:
        return self._flag.is_set()

    def engage(self, reason: str = "user requested") -> None:
        if not self._flag.is_set():
            log.warning("EMERGENCY STOP engaged", reason=reason)
        self._flag.set()

    def clear(self) -> None:
        if self._flag.is_set():
            log.info("emergency stop cleared")
        self._flag.clear()

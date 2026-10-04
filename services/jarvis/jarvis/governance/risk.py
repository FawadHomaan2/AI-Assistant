"""Risk tiers — the first axis of the permission model (ARCHITECTURE §7).

A tier is a property of what an action *does*, not of who asked for it. Tiers may
be raised by the user but never silently lowered, and tier 5 can never be
auto-approved by any setting.
"""

from __future__ import annotations

from enum import IntEnum


class Risk(IntEnum):
    SAFE = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    CRITICAL = 5

    @property
    def label(self) -> str:
        return {
            Risk.SAFE: "safe",
            Risk.LOW: "low",
            Risk.MEDIUM: "medium",
            Risk.HIGH: "high",
            Risk.CRITICAL: "critical",
        }[self]

    @property
    def allows_remember(self) -> bool:
        """High and critical actions are never remembered.

        The gate's value collapses if a user can click "always allow" on a
        permanent delete, so the option is not offered above medium.
        """
        return self <= Risk.MEDIUM

    @property
    def needs_typed_phrase(self) -> bool:
        return self >= Risk.CRITICAL


#: Confirmation is required when the action's tier exceeds the mode's ceiling.
#: Nothing lifts the ceiling to 5: critical always asks.
MODE_AUTO_CEILING: dict[str, Risk] = {
    "paused": Risk.SAFE,  # nothing executes at all; see Policy.evaluate
    "guarded": Risk.LOW,  # safe + low run; medium and up confirm
    "assisted": Risk.MEDIUM,  # medium runs inside granted scopes
    "developer": Risk.MEDIUM,  # same ceiling, wider command allowlist
}

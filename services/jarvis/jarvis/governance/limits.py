"""Rate limits — the backstop behind the permission model.

Scopes and confirmations assume a human in the loop. A confused agent loop does
not have one: it can propose the same action a thousand times a minute, and a
user who has clicked "allow" once on a medium-risk action will not notice the
four hundredth. Limits are what make a runaway loop stop rather than grind.

Two separate things are limited, because they fail differently:

  **Actions per minute**, per risk tier. Catches the loop. Higher tiers get far
  smaller budgets, because a hundred file reads a minute is a busy assistant
  and a hundred permanent deletes a minute is a disaster.

  **Consent prompts per minute.** Catches prompt fatigue being used as an
  attack: bury the one that matters in forty that do not, and it gets approved.

A limit is a refusal, never a queue. Silently delaying an action the user asked
for would make Jarvis feel broken and would hide the loop that caused it.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from jarvis.governance.risk import Risk
from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: Actions per minute, by tier. Generous where mistakes are cheap and tight
#: where they are not.
PER_MINUTE: dict[Risk, int] = {
    Risk.SAFE: 240,
    Risk.LOW: 120,
    Risk.MEDIUM: 40,
    Risk.HIGH: 10,
    Risk.CRITICAL: 3,
}

#: Consent prompts per minute, whatever their tier.
PROMPTS_PER_MINUTE = 20

WINDOW_SECONDS = 60.0


@dataclass
class LimitVerdict:
    allowed: bool
    reason: str = ""
    retry_after_seconds: float = 0.0


@dataclass
class RateLimiter:
    """Sliding windows, one per tier plus one for prompts."""

    per_minute: dict[Risk, int] = field(default_factory=lambda: dict(PER_MINUTE))
    prompts_per_minute: int = PROMPTS_PER_MINUTE
    _actions: dict[Risk, deque[float]] = field(default_factory=dict)
    _prompts: deque[float] = field(default_factory=deque)

    @staticmethod
    def _now() -> float:
        # Monotonic: a clock change must not hand out a fresh budget.
        return time.monotonic()

    def _window(self, risk: Risk) -> deque[float]:
        return self._actions.setdefault(risk, deque())

    @staticmethod
    def _trim(window: deque[float], now: float) -> None:
        while window and now - window[0] >= WINDOW_SECONDS:
            window.popleft()

    def check(self, risk: Risk) -> LimitVerdict:
        """Whether an action of this tier may run. Does not consume budget."""
        now = self._now()
        window = self._window(risk)
        self._trim(window, now)
        allowance = self.per_minute.get(risk, 60)
        if len(window) < allowance:
            return LimitVerdict(allowed=True)
        # An allowance of zero leaves the window empty, so there is no first
        # entry to wait for. Reading window[0] here crashed the gate — and a
        # crash in the limiter is a bypass, because the caller never gets a
        # verdict at all.
        wait = WINDOW_SECONDS - (now - window[0]) if window else WINDOW_SECONDS
        return LimitVerdict(
            allowed=False,
            reason=(
                f"That is {allowance} {risk.label}-risk actions in a minute, which is "
                f"this tier's limit. Jarvis has stopped rather than carrying on — a "
                f"burst like this usually means something is looping."
            ),
            retry_after_seconds=max(0.0, round(wait, 1)),
        )

    def record(self, risk: Risk) -> None:
        window = self._window(risk)
        now = self._now()
        self._trim(window, now)
        window.append(now)

    def check_prompt(self) -> LimitVerdict:
        now = self._now()
        self._trim(self._prompts, now)
        if len(self._prompts) < self.prompts_per_minute:
            return LimitVerdict(allowed=True)
        wait = WINDOW_SECONDS - (now - self._prompts[0]) if self._prompts else WINDOW_SECONDS
        return LimitVerdict(
            allowed=False,
            reason=(
                f"Jarvis has asked you to confirm {self.prompts_per_minute} things in a "
                f"minute. It has stopped asking: a flood of prompts is how a dangerous "
                f"one gets approved by accident."
            ),
            retry_after_seconds=max(0.0, round(wait, 1)),
        )

    def record_prompt(self) -> None:
        now = self._now()
        self._trim(self._prompts, now)
        self._prompts.append(now)

    def reset(self) -> None:
        self._actions.clear()
        self._prompts.clear()

    def describe(self) -> dict[str, object]:
        now = self._now()
        for window in self._actions.values():
            self._trim(window, now)
        self._trim(self._prompts, now)
        return {
            "windowSeconds": WINDOW_SECONDS,
            "perMinute": {r.label: n for r, n in self.per_minute.items()},
            "used": {r.label: len(w) for r, w in self._actions.items() if w},
            "promptsPerMinute": self.prompts_per_minute,
            "promptsUsed": len(self._prompts),
        }

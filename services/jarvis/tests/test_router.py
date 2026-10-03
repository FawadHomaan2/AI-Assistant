from __future__ import annotations

import pytest

from jarvis.agents.router import route
from jarvis.agents.types import Intent


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Commands to act on the machine.
        ("Open Chrome", Intent.COMPUTER_TASK),
        ("open vs code and create a project", Intent.COMPUTER_TASK),
        ("Create a new folder on my desktop", Intent.COMPUTER_TASK),
        ("Organize my Downloads folder", Intent.COMPUTER_TASK),
        ("rename these files", Intent.COMPUTER_TASK),
        # Screenshots are a question about this machine, handled by the
        # diagnostics route since Phase 5.
        ("take a screenshot", Intent.DIAGNOSTIC),
        ("Find duplicate files", Intent.COMPUTER_TASK),
        ("find my CV files", Intent.COMPUTER_TASK),
        ("close this application", Intent.COMPUTER_TASK),
        ("turn off bluetooth", Intent.COMPUTER_TASK),
        # Machine condition.
        ("why is my laptop slow?", Intent.DIAGNOSTIC),
        ("tell me which programs are running", Intent.DIAGNOSTIC),
        ("how much disk space do I have", Intent.DIAGNOSTIC),
        # Security posture.
        ("check my computer's security", Intent.SECURITY),
        ("is anything suspicious happening?", Intent.SECURITY),
        ("is my firewall on", Intent.SECURITY),
        # Preferences.
        ("remember that I prefer VS Code", Intent.MEMORY),
        ("from now on use Firefox", Intent.MEMORY),
        # Genuine conversation.
        ("hello", Intent.CHAT),
        ("explain how React hooks work", Intent.CHAT),
        ("what is a firewall?", Intent.CHAT),
        ("how do I find files in Windows?", Intent.CHAT),
        ("who wrote Dune?", Intent.CHAT),
    ],
)
def test_classification(message: str, expected: Intent) -> None:
    assert route(message).intent is expected, f"{message!r} misrouted"


def test_empty_message_is_chat() -> None:
    assert route("").intent is Intent.CHAT


def test_decision_explains_itself() -> None:
    """The UI shows the reason, so it must never be empty."""
    decision = route("Open Chrome")
    assert decision.reason
    assert decision.signals
    assert 0.0 < decision.confidence <= 1.0

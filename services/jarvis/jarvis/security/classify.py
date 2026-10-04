"""Deciding what a finding is allowed to claim.

A pure function, deliberately. Classification is the one place where a security
tool is most tempted to overstate, and the temptation is strongest when the
decision is scattered through the checks — each one reasonably deciding that
*its* unfamiliar thing looks a bit dodgy. Centralising it means the rule is
written once, tested directly, and visible to anyone reading the code.

The rule:

  known-bad pattern matched        → SUSPICIOUS   (and the pattern is named)
  a measured fact, stated plainly  → CONFIRMED
  a protection off / weakness      → POTENTIAL
  anything else, including new     → NORMAL

Novelty never raises a classification on its own. "I have not seen this before"
is information; it is not evidence of wrongdoing, and a tool that treats it as
such teaches its user to dismiss alerts.
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.security.types import Classification, Severity


@dataclass(frozen=True)
class Signals:
    """The measured inputs to a classification. All of them are facts."""

    #: A specific risky pattern was matched, named here. This is the ONLY thing
    #: that can produce SUSPICIOUS, and the name goes into the evidence so the
    #: claim can be argued with.
    matched_pattern: str = ""
    #: A protection is off, or a setting is weaker than it should be.
    weakness: bool = False
    #: Something observably happened or is observably true.
    observed_fact: bool = False
    #: Not present in the baseline. Information, never an accusation.
    novel: bool = False
    #: The user has marked this as fine.
    trusted: bool = False


def classify(signals: Signals) -> Classification:
    """What a finding may claim, given what was actually measured."""
    if signals.trusted:
        # The user has already said this is fine. Continuing to flag it is how
        # a security tool becomes noise.
        return Classification.NORMAL
    if signals.matched_pattern:
        return Classification.SUSPICIOUS
    if signals.weakness:
        return Classification.POTENTIAL
    if signals.observed_fact:
        return Classification.CONFIRMED
    return Classification.NORMAL


def severity_for(classification: Classification, *, base: Severity) -> Severity:
    """Cap severity at what the classification can justify.

    A `NORMAL` finding is never worse than informational, whatever a check
    thought. This stops "it is new, so let us call it high severity" from
    arriving through the back door after `classify` refused it the front one.
    """
    if classification is Classification.NORMAL:
        return Severity.INFO
    if classification is Classification.POTENTIAL and base.rank > Severity.HIGH.rank:
        # Nothing has happened yet. "Critical" belongs to things that have.
        return Severity.HIGH
    return base


def novelty_note(novel: bool, label: str) -> str:
    """The sentence that says "new" without saying "bad"."""
    if not novel:
        return ""
    return (
        f"This is the first time Jarvis has seen {label} on this computer. "
        f"That is worth knowing, but it is not evidence of a problem — new "
        f"software, a new device or a first scan all look like this."
    )

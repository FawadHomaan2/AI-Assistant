"""Working out what is worth remembering.

Rule-based, like the router and planner, and for the same reasons: it is
instant, it works with no model configured, and it is deterministic enough to
test. A model-assisted pass would catch more phrasings, and would also invent
preferences the user never expressed — which is much worse here than in
routing, because a wrong memory persists and shapes every later turn.

So this errs towards extracting nothing. A statement it does not recognise
becomes an ordinary conversation turn rather than a guessed belief, and the
user can always say "remember that ..." to be explicit.

Two kinds of learning:

  **Stated** — "always open PDFs in Acrobat". Recognised here, acted on at once.
  **Observed** — Jarvis notices the same choice three times. Produced by
  `from_tool_use`, and never acted on until the store promotes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Phrases that mean "this is a durable preference", not just conversation.
_STATEMENT = re.compile(
    r"\b(?:"
    r"always|never|from now on|in future|in the future|going forward"
    r"|i (?:prefer|like|want|always|usually)"
    r"|my (?:preferred|favourite|favorite|default|usual)"
    r"|remember (?:that|this|to)?"
    r"|don'?t forget"
    r"|make (?:that|this) (?:the )?default"
    r")\b",
    re.IGNORECASE,
)

_FORGET = re.compile(
    r"\b(?:forget (?:that|about|my)?|stop remembering|don'?t remember|unlearn"
    r"|that'?s wrong|that is wrong|no longer)\b",
    re.IGNORECASE,
)

#: "open PDFs in Acrobat", "open pdf files with Edge".
_APP_FOR_TYPE = re.compile(
    r"\bopen\s+(?:my\s+|all\s+)?(?P<kind>[a-z0-9]{2,12})s?\s+(?:files?\s+)?"
    r"(?:in|with|using)\s+(?P<app>[\w .+\-]{2,40}?)\s*(?:\.|$|,|\band\b)",
    re.IGNORECASE,
)

#: "save screenshots to Pictures", "put downloads in Documents".
_DEFAULT_FOLDER = re.compile(
    r"\b(?:save|put|store|keep)\s+(?:my\s+)?(?P<what>[a-z0-9 ]{3,24}?)\s+"
    r"(?:in|to|into|under)\s+(?:my\s+|the\s+)?(?P<folder>[\w \-/\\:.]{2,60}?)"
    r"\s*(?:\.|$|,)",
    re.IGNORECASE,
)

#: "call me Fawad", "my name is Fawad".
_NAME = re.compile(
    r"\b(?:call me|my name is|i'?m called|i am called)\s+(?P<name>[A-Z][\w\-']{1,30})",
)

#: "I prefer dark mode", "use the light theme".
_THEME = re.compile(r"\b(?P<theme>dark|light)\s+(?:mode|theme|colou?rs?)\b", re.IGNORECASE)

#: "be brief", "keep answers short", "explain things in detail".
_STYLE = re.compile(
    r"\b(?P<style>brief|concise|short|detailed|thorough|technical|simple|plain)\b"
    r"(?:\s+(?:answers?|replies|responses|explanations?))?",
    re.IGNORECASE,
)

#: Words that would make a useless memory key on their own.
_FILLER = frozenset(
    {
        "that", "this", "it", "the", "a", "an", "to", "me", "my", "i", "is",
        "are", "and", "for", "please", "jarvis", "remember", "always", "never",
        "from", "now", "on", "in", "future", "prefer", "like", "want", "use",
        "using", "with", "when", "you", "your", "should", "would", "do",
    }
)  # fmt: skip

_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Statement:
    """Something worth storing, and where it goes."""

    tier: str
    key: str
    value: Any
    #: What the interface shows back: "I'll open PDFs in Acrobat from now on."
    confirmation: str


def slug(text: str, limit: int = 4) -> str:
    """A stable key from free text.

    Stability is the whole point: "remember I work at night" said twice must
    produce the same key, or the second one creates a duplicate instead of
    corroborating the first.
    """
    words = [w for w in _WORD.findall(text.lower()) if w not in _FILLER and len(w) > 1]
    return "-".join(words[:limit]) or "note"


def is_forget(message: str) -> bool:
    return bool(_FORGET.search(message))


def is_statement(message: str) -> bool:
    return bool(_STATEMENT.search(message))


def extract(message: str) -> list[Statement]:
    """Pull durable preferences out of a message, or return nothing.

    Most patterns only run on messages that look like statements of preference
    ("always", "from now on", "I prefer", "remember that"). Without that gate,
    "open the pdf in acrobat" — a one-off request — would be recorded as a
    standing preference and Jarvis would start doing it forever.

    A few patterns are unambiguous on their own and skip the gate, because
    requiring a wrapper around them would mean the way people actually phrase
    the thing is the way that does not work.
    """
    text = message.strip()
    if not text or is_forget(text):
        return []

    found: list[Statement] = []

    # "call me Fawad" needs no preference wrapper around it — it is already an
    # unambiguous statement about the user, and requiring "always" or
    # "remember that" would mean the most natural phrasing is the one that
    # does not work.
    if match := _NAME.search(text):
        name = match.group("name")
        found.append(
            Statement(
                tier="semantic",
                key="user.name",
                value=name,
                confirmation=f"I'll call you {name}.",
            )
        )

    if not is_statement(text):
        return found

    if match := _APP_FOR_TYPE.search(text):
        kind = match.group("kind").lower().rstrip("s")
        app = match.group("app").strip().rstrip(".")
        found.append(
            Statement(
                tier="semantic",
                key=f"app.open.{kind}",
                value=app,
                confirmation=f"I'll open {kind} files in {app}.",
            )
        )

    if match := _THEME.search(text):
        theme = match.group("theme").lower()
        found.append(
            Statement(
                tier="semantic",
                key="preference.theme",
                value=theme,
                confirmation=f"Noted — you prefer {theme} mode.",
            )
        )

    if (match := _DEFAULT_FOLDER.search(text)) and not found:
        what = match.group("what").strip().lower()
        folder = match.group("folder").strip().rstrip(".")
        found.append(
            Statement(
                tier="semantic",
                key=f"folder.{slug(what, 2)}",
                value=folder,
                confirmation=f"I'll keep {what} in {folder}.",
            )
        )

    if (match := _STYLE.search(text)) and not found:
        style = match.group("style").lower()
        found.append(
            Statement(
                tier="semantic",
                key="preference.style",
                value=style,
                confirmation=f"I'll keep my answers {style}.",
            )
        )

    if not found:
        # Nothing specific matched, but the user clearly asked to be remembered.
        # Store the sentence itself rather than guessing at its structure — it
        # is visible and editable in the Privacy dashboard either way.
        note = _STATEMENT.sub("", text).strip(" ,.:;!?-").strip()
        if len(note) >= 3:
            found.append(
                Statement(
                    tier="semantic",
                    key=f"note.{slug(note)}",
                    value=note,
                    confirmation=f"Remembered: {note}",
                )
            )

    return found


def from_tool_use(tool: str, args: dict[str, Any], ok: bool) -> Statement | None:
    """An observation from something that actually happened.

    Only successful actions count. Learning from a failed launch would teach
    Jarvis a preference for an application that is not installed.
    """
    if not ok:
        return None

    if tool == "application" and args.get("operation") == "launch":
        name = str(args.get("name") or "").strip()
        if name:
            return Statement(
                tier="semantic",
                key="app.launched.favourite",
                value=name,
                confirmation=f"Noticed you started {name}.",
            )

    if tool == "browser" and args.get("operation") == "open":
        url = str(args.get("url") or "")
        host = re.sub(r"^https?://", "", url).split("/")[0].lower()
        if host:
            return Statement(
                tier="episodic",
                key=f"site.visited.{host}",
                value=host,
                confirmation=f"Noticed you visited {host}.",
            )

    if tool == "filesystem" and args.get("operation") in ("list", "search"):
        path = str(args.get("path") or "").strip().lower()
        if path and "/" not in path and "\\" not in path:
            return Statement(
                tier="semantic",
                key="folder.most-used",
                value=path,
                confirmation=f"Noticed you work in {path}.",
            )

    return None

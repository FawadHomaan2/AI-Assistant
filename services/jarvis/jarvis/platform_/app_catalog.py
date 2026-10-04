"""Finding installed applications, and matching a spoken name to one.

The security property that matters: **the model never supplies a path to
execute.** It supplies a name, which is matched against software this machine
actually has installed, and the catalogue entry supplies the launch target. A
request to "open C:\\Users\\me\\Downloads\\invoice.pdf.exe" therefore cannot
become a launch — there is no catalogue entry for it.

The matching itself is pure logic, so it is testable on any platform; only the
discovery of entries differs between Windows and POSIX.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jarvis.platform_.types import AppInfo

#: Names people use that differ from the installed name.
COMMON_ALIASES: dict[str, tuple[str, ...]] = {
    "chrome": ("google chrome", "chrome"),
    "edge": ("microsoft edge", "edge", "msedge"),
    "firefox": ("mozilla firefox", "firefox"),
    "vscode": ("visual studio code", "vs code", "vscode", "code"),
    "word": ("microsoft word", "word", "winword"),
    "excel": ("microsoft excel", "excel"),
    "powerpoint": ("microsoft powerpoint", "powerpoint", "ppt"),
    "outlook": ("microsoft outlook", "outlook"),
    "explorer": ("file explorer", "explorer", "windows explorer", "files"),
    "terminal": ("windows terminal", "terminal", "wt"),
    "powershell": ("windows powershell", "powershell", "pwsh"),
    "cmd": ("command prompt", "cmd"),
    "notepad": ("notepad",),
    "calculator": ("calculator", "calc"),
    "settings": ("settings", "windows settings"),
    "spotify": ("spotify",),
    "discord": ("discord",),
    "slack": ("slack",),
    "teams": ("microsoft teams", "teams"),
    "steam": ("steam",),
    "vlc": ("vlc", "vlc media player"),
}

_NOISE = re.compile(r"\b(app|application|program|the|please|my|a|an)\b", re.IGNORECASE)

#: Vendor and category words shared by unrelated products. Matching on these
#: alone would make "word" find "Microsoft Edge", so they are ignored when
#: scoring word overlap — though they still count in an exact or prefix match.
_GENERIC = frozenset(
    {
        "microsoft",
        "google",
        "mozilla",
        "apple",
        "adobe",
        "windows",
        "corporation",
        "inc",
        "ltd",
        "studio",
        "desktop",
        "client",
        "player",
        "viewer",
        "editor",
        "manager",
        "browser",
        "tool",
        "suite",
        "office",
        "pro",
        "plus",
        "lite",
    }
)
_PUNCT = re.compile(r"[^\w\s]")


def normalise(text: str) -> str:
    """Reduce a name to comparable words."""
    cleaned = _PUNCT.sub(" ", text.lower())
    cleaned = _NOISE.sub(" ", cleaned)
    return " ".join(cleaned.split())


@dataclass
class Match:
    app: AppInfo
    score: float
    reason: str


def _score(query: str, candidate: str) -> tuple[float, str]:
    """How well a query matches one candidate name. 0 means no match."""
    if not query or not candidate:
        return 0.0, ""
    if query == candidate:
        return 1.0, "exact name"

    query_words = query.split()
    candidate_words = candidate.split()

    if candidate.startswith(query) or query.startswith(candidate):
        return 0.9, "name prefix"
    # "chrome" inside "google chrome" is a strong signal; a substring inside a
    # longer word ("note" in "notepad") is weaker and handled below.
    distinctive = (set(query_words) & set(candidate_words)) - _GENERIC
    if distinctive:
        return 0.5 + 0.3 * len(distinctive) / max(len(query_words), 1), "shared word"
    if query in candidate:
        return 0.45, "contained in name"
    return 0.0, ""


def resolve(query: str, catalogue: list[AppInfo]) -> list[Match]:
    """Rank installed applications against a spoken name.

    Returns every plausible match, best first. The caller decides whether a
    single clear winner exists — guessing between "Word" and "WordPad" would
    open the wrong program.
    """
    wanted = normalise(query)
    if not wanted:
        return []

    # An alias group lets "vs code" find "Visual Studio Code".
    alias_terms: set[str] = {wanted}
    for terms in COMMON_ALIASES.values():
        if wanted in terms:
            alias_terms.update(terms)

    matches: list[Match] = []
    for app in catalogue:
        names = {normalise(app.name), normalise(app.key), *(normalise(a) for a in app.aliases)}
        best, reason = 0.0, ""
        for term in alias_terms:
            for name in names:
                score, why = _score(term, name)
                if score > best:
                    best, reason = score, why
        if best > 0:
            matches.append(Match(app, best, reason))

    matches.sort(key=lambda m: (-m.score, len(m.app.name)))
    return matches


def pick(query: str, catalogue: list[AppInfo]) -> tuple[AppInfo | None, list[Match]]:
    """Choose one app, or report that the choice is not clear.

    A clear winner is a top match that is meaningfully better than the runner-up.
    Otherwise the caller asks rather than guessing.
    """
    matches = resolve(query, catalogue)
    if not matches:
        return None, []
    if len(matches) == 1:
        return matches[0].app, matches
    best, second = matches[0], matches[1]
    # A small tolerance: these are float sums, so a gap of exactly 0.1 can
    # compute as 0.09999999999999998 and wrongly read as ambiguous.
    epsilon = 1e-9
    gap = best.score - second.score
    if best.score >= 0.9 and gap >= 0.1 - epsilon:
        return best.app, matches
    if gap >= 0.25 - epsilon:
        return best.app, matches
    return None, matches

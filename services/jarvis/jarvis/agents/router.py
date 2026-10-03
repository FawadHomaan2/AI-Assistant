"""Intent routing.

Deliberately rule-based rather than model-based, for three reasons: it is
instant, it works with no model configured, and it is deterministic enough to
unit-test. A model-assisted pass for ambiguous input is a later refinement.

Its most important job right now is **not** classification quality — it is
preventing a configured language model from answering "Open Chrome" with "Done,
I've opened Chrome." No tools exist until Phase 3, so a request to act on the
computer must be intercepted here and answered honestly. Without this, adding a
capable model would make Jarvis start lying.
"""

from __future__ import annotations

import re

from jarvis.agents.types import Intent, Route

# Verbs that mean "change or inspect my machine", not "tell me about something".
_ACTION_VERBS = (
    "open",
    "launch",
    "start",
    "run",
    "close",
    "quit",
    "kill",
    "minimise",
    "minimize",
    "maximise",
    "maximize",
    "switch to",
    "focus",
    "create",
    "make a folder",
    "make a directory",
    "rename",
    "move",
    "copy",
    "delete",
    "remove",
    "organise",
    "organize",
    "clean up",
    "sort",
    "empty",
    "screenshot",
    "take a screen",
    "capture the screen",
    "type ",
    "click ",
    "scroll ",
    "press ",
    "download",
    "upload",
    "go to ",
    "goto ",
    "visit ",
    "browse ",
    "navigate to",
    "google ",
    "look up",
    "search the web",
    "search online",
    "pull up",
    "install",
    "uninstall",
    "find ",
    "search for",
    "look for",
    "locate ",
    "list my",
    "show me my",
    "turn on",
    "turn off",
    "enable",
    "disable",
    "mute",
    "unmute",
    "set a reminder",
    "remind me",
)

_DIAGNOSTIC = (
    "why is my",
    "why's my",
    "running slow",
    "slow computer",
    "cpu usage",
    "ram usage",
    "memory usage",
    "disk space",
    "disk usage",
    "what's running",
    "whats running",
    "what is running",
    "what programs are open",
    "what apps are open",
    "programs are running",
    "apps are running",
    "applications are running",
    "programs running",
    "task manager",
    "using my cpu",
    "using my memory",
    "using my ram",
    "hogging",
    "which programs",
    "what programs",
    "running processes",
    "task manager",
    "battery",
    "temperature",
    "troubleshoot",
    "diagnose",
    "performance",
)

_SECURITY = (
    "virus",
    "malware",
    "antivirus",
    "defender",
    "firewall",
    "suspicious",
    "security",
    "hacked",
    "breach",
    "startup programs",
    "startup apps",
    "is anything wrong",
    "safe",
    "encrypted",
    "bitlocker",
    "phishing",
)

#: Questions about what is running. A substring list is too brittle for the
#: number of ways people phrase this ("which apps are open", "what applications
#: are currently running"), so this one gets a pattern.
#: Questions about the machine's own condition that now have tools behind them.
_MACHINE_QUESTION = re.compile(
    r"\b(?:"
    r"(?:is|are)\s+(?:my|the)\s+(?:internet|wi-?fi|network|connection)\b"
    r"|(?:can'?t|cannot)\s+(?:connect|get\s+online)"
    r"|check\s+(?:my\s+)?(?:memory|ram|cpu|disk|storage|battery|network|internet)"
    r"|how\s+much\s+(?:memory|ram|disk|storage|space)"
    r"|take\s+a\s+screen\s?shot|screenshot"
    r"|diagnose\b|run\s+a?\s*diagnostic"
    r")\b",
    re.IGNORECASE,
)

_PROCESS_QUESTION = re.compile(
    r"\b(?:"
    r"what(?:'s| is)?\s+running"
    r"|(?:what|which|list|show)\s+(?:\w+\s+){0,3}?(?:programs?|apps?|applications?|processes)\b"
    r"|running\s+(?:programs?|processes|apps?)"
    r"|task\s+manager"
    r"|(?:using|hogging|eating)\s+(?:my\s+)?(?:cpu|memory|ram)"
    r")\b",
    re.IGNORECASE,
)

_MEMORY = (
    "remember that",
    "remember this",
    "don't forget",
    "dont forget",
    "from now on",
    "always use",
    "always open",
    "my preferred",
    "i prefer",
    "forget that",
    "stop remembering",
)

#: Signals that a request is about the web rather than this machine. Checked
#: before the file branches so "go to example.com" is not read as a filename.
_WEB = re.compile(
    r"\b(?:"
    r"https?://"
    r"|www\."
    r"|(?:go to|goto|visit|browse(?: to)?|navigate to|open|load|pull up)\s+"
    r"(?:the\s+)?(?:web\s?site|web\s?page|url|link|[\w\-]+\.(?:com|org|net|io|dev|co|ai|app|gov|edu|uk))"
    r"|google(?:\s+(?:for|it|the))?\s"
    r"|search\s+(?:the\s+)?(?:web|internet|online)"
    r"|web\s+search"
    r"|look\s+up\b.{0,60}?\b(?:online|on the web|on the internet)"
    r"|\bon\s+(?:the\s+)?(?:web|internet)\b"
    r")",
    re.IGNORECASE,
)

_FILE_NOUNS = (
    "file",
    "files",
    "folder",
    "folders",
    "directory",
    "desktop",
    "downloads",
    "documents",
    "pdf",
    "docx",
    "xlsx",
    "screenshot",
    "duplicate",
)

#: A filename with an extension is a strong signal that a request is about a
#: file, whatever the surrounding phrasing. Without this, "read budget.csv"
#: looks conversational and never reaches the tools.
_FILENAME = re.compile(
    r"\b[\w\-]{1,60}\.(?:pdf|docx?|xlsx?|pptx?|txt|csv|tsv|md|json|log)\b", re.IGNORECASE
)

#: Verbs that mean "open this document" only when a file or path is also named.
#: On their own they are ordinary English ("read me a poem").
_READ_VERBS = ("read ", "summarise ", "summarize ", "open ")

#: An explicit path is routed to the tool layer so the path jail visibly refuses
#: it, rather than the model merely talking about the file.
_EXPLICIT_PATH = re.compile(r"(?:^|\s)(?:[A-Za-z]:[\\/]|/[a-z]|\.{1,2}[\\/]|~[\\/])", re.IGNORECASE)

# Questions *about* a topic are chat, even when they contain an action verb:
# "how do I open a port" is a question, "open chrome" is a command.
_QUESTION_PREFIX = re.compile(
    r"^\s*(what|what's|whats|who|when|where|why|how|which|can you explain|explain|"
    r"tell me about|is it|are there|should i|does|do you|did|would|could you explain)\b",
    re.IGNORECASE,
)


def _hits(text: str, needles: tuple[str, ...]) -> list[str]:
    return [n for n in needles if n in text]


def route(message: str) -> Route:
    """Classify a user message."""
    text = message.lower().strip()
    if not text:
        return Route(Intent.CHAT, 1.0, "empty message")

    is_question = bool(_QUESTION_PREFIX.match(text))

    if signals := _hits(text, _MEMORY):
        return Route(Intent.MEMORY, 0.85, "asks Jarvis to remember or forget something", signals)

    if signals := _hits(text, _SECURITY):
        # "what is a firewall" is a question about security, not a scan request.
        if is_question and not any(
            w in text for w in ("my ", "this computer", "this pc", "check", "scan")
        ):
            return Route(Intent.CHAT, 0.7, "a general question that mentions security", signals)
        return Route(Intent.SECURITY, 0.8, "asks about this machine's security state", signals)

    # Web requests are computer tasks, but they must be recognised before the
    # machine-condition and file branches: "open github.com" is not a file, and
    # "search the web for disk space tools" is not a diagnostic.
    if _WEB.search(text):
        return Route(
            Intent.COMPUTER_TASK,
            0.85,
            "asks Jarvis to visit or search the web",
            ["web request"],
        )

    if _MACHINE_QUESTION.search(text):
        return Route(
            Intent.DIAGNOSTIC,
            0.85,
            "asks about this computer's own condition",
            ["machine question"],
        )

    if _PROCESS_QUESTION.search(text):
        return Route(
            Intent.DIAGNOSTIC,
            0.85,
            "asks what is running on this computer",
            ["process question"],
        )

    if signals := _hits(text, _DIAGNOSTIC):
        return Route(Intent.DIAGNOSTIC, 0.8, "asks about this machine's condition", signals)

    action_signals = _hits(text, _ACTION_VERBS)
    if filename := _FILENAME.search(text):
        return Route(
            Intent.COMPUTER_TASK,
            0.9,
            "names a file, so this is about the user's own files",
            [*action_signals, filename.group(0).strip()],
        )

    if (action_signals or _hits(text, _READ_VERBS)) and _EXPLICIT_PATH.search(message):
        return Route(
            Intent.COMPUTER_TASK,
            0.9,
            "names an explicit path",
            action_signals,
        )

    if action_signals:
        starts_with_verb = any(text.startswith(v.strip()) for v in _ACTION_VERBS)
        # A question only counts as a command when it is about the user's own
        # things: "how do I find files in Windows" is informational,
        # "find my CV files" is a request to act.
        owns_the_subject = not is_question or " my " in f" {text} "
        mentions_files = bool(_hits(text, _FILE_NOUNS)) and owns_the_subject
        if starts_with_verb or mentions_files or not is_question:
            confidence = 0.9 if starts_with_verb else 0.7
            return Route(
                Intent.COMPUTER_TASK,
                confidence,
                "asks Jarvis to act on this computer",
                action_signals,
            )

    return Route(Intent.CHAT, 0.6, "conversational or informational", [])

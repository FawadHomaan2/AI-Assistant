"""Turning a request into tool calls.

Phase 3 handles the filesystem intents a rule-based mapper can resolve
unambiguously. Anything it cannot map is reported as not-yet-supported rather
than guessed at — a wrong guess here moves or deletes the wrong files.

Model-driven planning (the full DAG + critic described in ARCHITECTURE §8)
arrives once there are enough tools for a plan to be interesting. Keeping this
deterministic for now means the first phase that can modify files does so only
on requests whose meaning is unambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jarvis.config import folders


@dataclass
class Step:
    tool: str
    args: dict[str, Any]
    rationale: str


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    #: Set when the request was understood but cannot be turned into steps.
    unsupported: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [
                {"tool": s.tool, "args": s.args, "rationale": s.rationale} for s in self.steps
            ],
            "unsupported": self.unsupported,
        }


_FOLDER_WORDS = "|".join(folders.KNOWN_FOLDERS)

_LIST = re.compile(rf"\b(list|show|open|what(?:'s| is) in)\b.*\b({_FOLDER_WORDS})\b", re.IGNORECASE)
_SEARCH = re.compile(
    rf"\b(find|search for|look for|locate|list)\b.*\b(?P<folder>{_FOLDER_WORDS})\b",
    re.IGNORECASE,
)
#: File types the user might name, mapped to the glob they mean. Matched
#: separately from the sentence structure, which is too varied to capture in one
#: expression without it becoming unreadable and fragile.
_EXTENSIONS = {
    "pdf": "*.pdf",
    "pdfs": "*.pdf",
    "word": "*.doc*",
    "docx": "*.docx",
    "doc": "*.doc*",
    "excel": "*.xls*",
    "xlsx": "*.xlsx",
    "spreadsheet": "*.xls*",
    "powerpoint": "*.ppt*",
    "pptx": "*.pptx",
    "text": "*.txt",
    "txt": "*.txt",
    "csv": "*.csv",
    "image": "*.{jpg,jpeg,png}",
    "images": "*.{jpg,jpeg,png}",
    "photo": "*.jpg",
    "photos": "*.jpg",
    "jpg": "*.jpg",
    "png": "*.png",
    "zip": "*.zip",
    "video": "*.mp4",
    "videos": "*.mp4",
    "music": "*.mp3",
}
_EXT_WORDS = re.compile(
    r"\b(" + "|".join(sorted(_EXTENSIONS, key=len, reverse=True)) + r")\b", re.IGNORECASE
)
_CREATE_FOLDER = re.compile(
    rf"\b(create|make|add)\b.*\bfolder\b.*?(?:called|named)\s+"
    rf"[\"']?(?P<name>[\w \-]+?)[\"']?\s*(?:\b(?:in|on|inside)\b\s+"
    rf"(?:my\s+)?(?P<folder>{_FOLDER_WORDS}))?\s*$",
    re.IGNORECASE,
)
_DUPLICATES = re.compile(
    rf"\b(duplicate|duplicates)\b.*\b(?P<folder>{_FOLDER_WORDS})\b|"
    rf"\b(?P<folder2>{_FOLDER_WORDS})\b.*\b(duplicate|duplicates)\b",
    re.IGNORECASE,
)
#: Deleting is mapped so the request reaches the permission gate, which refuses
#: it by default — `fs.delete` is not granted on a fresh install. A silent "I
#: don't understand" would hide the fact that the capability exists but is off.
_DELETE = re.compile(
    rf"\b(delete|remove|bin|trash)\b\s+(?:the\s+)?(?P<name>[\w\-. ]+?\.\w{{2,5}})"
    rf"(?:\s+(?:from|in)\s+(?:my\s+)?(?P<folder>{_FOLDER_WORDS}))?\s*$",
    re.IGNORECASE,
)

#: "open chrome", "launch spotify".
_LAUNCH = re.compile(
    r"\b(?:open|launch|start|run)\b\s+(?:up\s+)?(?:the\s+|my\s+)?(?P<name>[\w .+\-]{2,40}?)"
    r"\s*(?:\b(?:app|application|program|for me|please)\b.*)?$",
    re.IGNORECASE,
)

#: "what's running", "which programs are open", "what is using my cpu".
_PROCESS_LIST = re.compile(
    r"\b(?:"
    r"what(?:'s| is)?\s+running"
    # "what programs are running", "which apps are open", "what applications
    # are currently running" — up to two words between the noun and the verb.
    r"|(?:what|which|list)\s+(?:\w+\s+){0,2}?(?:programs?|apps?|applications?|processes)\b"
    r"|running\s+(?:programs?|processes|apps?)"
    r"|task\s+manager"
    r")\b",
    re.IGNORECASE,
)

#: "what's using my cpu", "what's slowing things down".
_PROCESS_TOP = re.compile(
    r"\b(using\s+(?:my\s+)?(?:cpu|memory|ram)|most\s+(?:cpu|memory|ram)|"
    r"hogging|eating\s+(?:my\s+)?(?:cpu|memory|ram)|busiest)\b",
    re.IGNORECASE,
)

#: "why is my computer slow".
_SLOW = re.compile(
    r"\b(?:why\s+(?:is|are|'s)\s+(?:my|this|the)\b.{0,24}?\b"
    r"(?:slow|sluggish|laggy|lagging|freezing|stuttering)"
    r"|(?:computer|pc|laptop|machine)\s+(?:is\s+)?(?:so\s+)?(?:slow|sluggish|laggy)"
    r"|what(?:'s| is)\s+(?:making|slowing)\b"
    r"|diagnose\b|run\s+a?\s*diagnostic)",
    re.IGNORECASE,
)

#: "is my internet working", "why can't I connect".
_CONNECTIVITY = re.compile(
    r"\b(?:internet|wi-?fi|network|connection)\b.{0,24}?\b"
    r"(?:working|down|up|broken|connected|problem)"
    r"|\b(?:can'?t|cannot)\s+(?:connect|get online|reach)",
    re.IGNORECASE,
)

#: "take a screenshot".
_SCREENSHOT = re.compile(
    r"\b(?:take|grab|capture|get)\s+(?:a\s+)?screen\s?shot|\bscreenshot\b", re.IGNORECASE
)

#: "how much disk space do I have", "what's my battery at".
_SYSTEM_INFO = re.compile(
    r"\b(?:how much|what(?:'s| is)|check(?: my)?|show(?: me)?)\b.{0,20}?"
    r"\b(?P<topic>disk|disks|storage|space|memory|ram|cpu|processor|battery|uptime)\b",
    re.IGNORECASE,
)

#: "close notepad", "minimise word".
_WINDOW_ACTION = re.compile(
    r"\b(?P<action>close|quit|minimi[sz]e|maximi[sz]e|focus|switch to|bring up)\b"
    r"\s+(?:the\s+|my\s+)?(?P<name>[\w .+\-]{2,40}?)"
    r"\s*(?:\b(?:window|app|application|program|please)\b.*)?$",
    re.IGNORECASE,
)

#: Words that mean the request is about files, not an application.
_FILE_CONTEXT = re.compile(
    r"\b(folder|file|files|document|directory|pdf|docx?|xlsx?|csv|txt)\b", re.IGNORECASE
)

_READ = re.compile(r"\b(read|open|summari[sz]e)\b.+?(?P<path>[\w\-. ]+\.\w{2,5})\b", re.IGNORECASE)

#: An explicit path with no recognised extension. Routed to the document tool so
#: the path jail answers with its specific reason rather than the planner
#: shrugging — "/etc/passwd is outside the folders Jarvis may use" is a far more
#: useful reply than "I could not work out the steps".
_READ_PATH = re.compile(
    r"\b(?:read|open|summari[sz]e)\b\s+(?P<path>(?:[A-Za-z]:[\\/]|/|~[\\/]|\.{1,2}[\\/])\S+)",
    re.IGNORECASE,
)

#: Requests that are clearly about something other than files, so the reply can
#: name the right missing capability instead of talking about folders.
_OTHER_DOMAINS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(drivers?|startup programs|startup apps|startup items|windows update)\b",
            re.IGNORECASE,
        ),
        "Driver, startup-item and Windows Update checks arrive with the Security "
        "Center in Phase 9. I can already measure CPU, memory, disks and uptime, "
        "and tell you what is running.",
    ),
    (
        re.compile(r"\b(wi-?fi|bluetooth|volume|brightness|turn (on|off))\b", re.IGNORECASE),
        "Changing Windows settings is not built yet — Jarvis can read system "
        "state but not reconfigure it.",
    ),
    (
        re.compile(r"\b(browse|website|web ?site|google|search online|the web)\b", re.IGNORECASE),
        "Browsing the web arrives in Phase 7.",
    ),
    (
        re.compile(r"\b(remind me|reminder|schedule)\b", re.IGNORECASE),
        "Reminders arrive with scheduled tasks after v1.",
    ),
)


def plan(message: str) -> Plan:
    """Map a filesystem request to tool calls, or explain that it cannot."""
    text = message.strip()

    if match := _DUPLICATES.search(text):
        folder = (match.group("folder") or match.group("folder2") or "downloads").lower()
        return Plan(
            [
                Step(
                    "filesystem",
                    {"operation": "find_duplicates", "path": folder},
                    f"scan {folder} for files with identical contents",
                )
            ]
        )

    if match := _CREATE_FOLDER.search(text):
        name = (match.group("name") or "").strip()
        folder = (match.groupdict().get("folder") or "desktop").lower()
        if name:
            return Plan(
                [
                    Step(
                        "filesystem",
                        {"operation": "create_folder", "path": f"{folder}/{name}"},
                        f"create {name!r} inside {folder}",
                    )
                ]
            )

    if match := _LIST.search(text):
        folder = match.group(2).lower()
        return Plan(
            [
                Step(
                    "filesystem",
                    {"operation": "list", "path": folder},
                    f"list the contents of {folder}",
                )
            ]
        )

    if match := _SEARCH.search(text):
        folder = (match.group("folder") or "documents").lower()
        # Ignore a file-type word that is itself the folder name ("my music").
        ext_match = next(
            (m for m in _EXT_WORDS.finditer(text) if m.group(1).lower() != folder),
            None,
        )
        pattern = _EXTENSIONS[ext_match.group(1).lower()] if ext_match else "*"
        args: dict[str, Any] = {"operation": "search", "path": folder, "pattern": pattern}
        if re.search(r"\blast month\b|\brecent(ly)?\b|\bthis week\b", text, re.IGNORECASE):
            args["modified_within_days"] = 31
        return Plan([Step("filesystem", args, f"search {folder} for {pattern}")])

    if _SLOW.search(text):
        return Plan(
            [
                Step(
                    "diagnostics",
                    {"operation": "performance"},
                    "measure CPU, memory, disks and uptime, and report what crossed a threshold",
                )
            ]
        )

    if _CONNECTIVITY.search(text):
        return Plan(
            [
                Step(
                    "diagnostics",
                    {"operation": "connectivity"},
                    "test whether the internet and DNS are working",
                )
            ]
        )

    if _SCREENSHOT.search(text):
        return Plan([Step("screenshot", {}, "capture the screen")])

    if _PROCESS_TOP.search(text):
        return Plan(
            [
                Step(
                    "process",
                    {"operation": "top", "limit": 10},
                    "measure which programs are using the CPU",
                )
            ]
        )

    if _PROCESS_LIST.search(text):
        return Plan(
            [
                Step(
                    "process",
                    {"operation": "list", "limit": 20},
                    "list the running programs",
                )
            ]
        )

    if match := _SYSTEM_INFO.search(text):
        topic = match.group("topic").lower()
        operation = {
            "disk": "disks",
            "disks": "disks",
            "storage": "disks",
            "space": "disks",
            "memory": "memory",
            "ram": "memory",
            "cpu": "cpu",
            "processor": "cpu",
            "battery": "battery",
            "uptime": "uptime",
        }.get(topic, "summary")
        return Plan([Step("systeminfo", {"operation": operation}, f"read {topic} information")])

    if match := _DELETE.search(text):
        name = match.group("name").strip()
        folder = (match.groupdict().get("folder") or "downloads").lower()
        return Plan(
            [
                Step(
                    "filesystem",
                    {"operation": "delete", "path": f"{folder}/{name}"},
                    f"move {name} from {folder} to the Recycle Bin",
                )
            ]
        )

    if match := _READ.search(text):
        return Plan(
            [
                Step(
                    "document",
                    {"operation": "read", "path": match.group("path").strip()},
                    f"read {match.group('path').strip()}",
                )
            ]
        )

    # Named distinctly from the search branch's `pattern`, which is a glob string.
    if match := _READ_PATH.search(text):
        target = match.group("path").strip()
        return Plan(
            [
                Step(
                    "document",
                    {"operation": "read", "path": target},
                    f"read {target}",
                )
            ]
        )

    # Window actions before launching, so "close notepad" is not read as "open".
    if (match := _WINDOW_ACTION.search(text)) and not _FILE_CONTEXT.search(text):
        action = match.group("action").lower()
        operation = {
            "close": "close",
            "quit": "close",
            "minimise": "minimise",
            "minimize": "minimise",
            "maximise": "maximise",
            "maximize": "maximise",
            "focus": "focus",
            "switch to": "focus",
            "bring up": "focus",
        }[action]
        name = match.group("name").strip()
        if name:
            return Plan(
                [
                    Step(
                        "window",
                        {"operation": operation, "title": name},
                        f"{operation} the {name} window",
                    )
                ]
            )

    if (match := _LAUNCH.search(text)) and not _FILE_CONTEXT.search(text):
        name = match.group("name").strip()
        if name and name.lower() not in {"up", "it", "that", "this"}:
            return Plan(
                [
                    Step(
                        "application",
                        {"operation": "launch", "name": name},
                        f"start {name}",
                    )
                ]
            )

    for domain_regex, explanation in _OTHER_DOMAINS:
        if domain_regex.search(text):
            return Plan(
                unsupported=(
                    f"{explanation} Right now I can work with your files: listing a "
                    f"folder, searching by type or date, creating a folder, reading a "
                    f"document, and finding duplicates."
                )
            )

    return Plan(
        unsupported=(
            "I understood that you want me to do something with your files, but I "
            "can't yet work out the exact steps from that phrasing. Right now I can "
            "list a folder, search for files by type or date, create a folder, read a "
            "document, and find duplicates. Multi-step planning from free-form "
            "instructions needs the model-driven planner, which is a later phase — "
            "and I'd rather say so than guess and move the wrong files."
        )
    )

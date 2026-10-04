"""PowerShellTool — a deliberately narrow door.

PowerShell can do anything, which makes "let the model write a PowerShell
command" equivalent to handing it the machine. This tool is therefore
**allowlist-only**: a fixed set of read-only cmdlets, each with validated
arguments. Everything else is refused, including anything that merely looks
clever.

The design rule is default-deny. A new capability means adding a cmdlet to the
list after thinking about it, not loosening the parser. Writing a blocklist of
dangerous commands would be the wrong shape — there are always more.

The validation logic is pure and fully tested here. Actually running PowerShell
needs Windows.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any

from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.tools.base import Preview, Tool, ToolError, ToolResult, ToolSpec
from jarvis.util.logging import get_logger
from jarvis.util.subprocess import run_command

log = get_logger(__name__)


def _on_windows() -> bool:
    """Read through a function so mypy keeps both branches type-checked.

    Referencing `sys.platform` directly lets it narrow to the host platform and
    declare the other branch unreachable, which hides real errors in it.
    """
    return sys.platform == "win32"


@dataclass(frozen=True)
class AllowedCmdlet:
    name: str
    description: str
    #: Parameters that may be passed. Anything else is refused.
    parameters: frozenset[str] = frozenset()
    #: True when the cmdlet reads state and changes nothing.
    read_only: bool = True


#: Every cmdlet Jarvis may run, and the switches each accepts. Read-only by
#: design: this tool answers questions, it does not reconfigure Windows.
ALLOWED: dict[str, AllowedCmdlet] = {
    c.name.lower(): c
    for c in (
        AllowedCmdlet("Get-ComputerInfo", "Operating system and hardware summary"),
        AllowedCmdlet("Get-CimInstance", "WMI/CIM query", frozenset({"-ClassName", "-Namespace"})),
        AllowedCmdlet("Get-Service", "Windows services", frozenset({"-Name", "-DisplayName"})),
        AllowedCmdlet("Get-Process", "Running processes", frozenset({"-Name", "-Id"})),
        AllowedCmdlet("Get-NetAdapter", "Network adapters", frozenset({"-Name"})),
        AllowedCmdlet("Get-NetIPConfiguration", "IP configuration"),
        AllowedCmdlet("Get-NetFirewallProfile", "Firewall profile state", frozenset({"-Name"})),
        AllowedCmdlet("Get-MpComputerStatus", "Microsoft Defender status"),
        AllowedCmdlet("Get-MpPreference", "Defender preferences"),
        AllowedCmdlet("Get-BitLockerVolume", "Disk encryption state", frozenset({"-MountPoint"})),
        AllowedCmdlet("Get-HotFix", "Installed updates", frozenset({"-Id"})),
        AllowedCmdlet("Get-StartApps", "Start Menu applications"),
        AllowedCmdlet("Get-PnpDevice", "Devices", frozenset({"-Class", "-Status"})),
        AllowedCmdlet("Get-Volume", "Volumes", frozenset({"-DriveLetter"})),
        AllowedCmdlet("Get-PhysicalDisk", "Physical disks"),
        AllowedCmdlet(
            "Get-WinEvent",
            "Event log",
            frozenset({"-LogName", "-MaxEvents", "-FilterHashtable"}),
        ),
        AllowedCmdlet("Get-LocalUser", "Local accounts", frozenset({"-Name"})),
        AllowedCmdlet(
            "Get-ScheduledTask", "Scheduled tasks", frozenset({"-TaskName", "-TaskPath"})
        ),
        AllowedCmdlet(
            "Get-ItemProperty", "Registry or file properties", frozenset({"-Path", "-Name"})
        ),
        AllowedCmdlet("Get-Help", "Cmdlet documentation", frozenset({"-Name"})),
    )
}

#: Formatting cmdlets that may appear after a pipe. They shape output only.
ALLOWED_FORMATTERS = frozenset(
    {
        "select-object",
        "sort-object",
        "where-object",
        "measure-object",
        "format-list",
        "format-table",
        "convertto-json",
        "select",
        "sort",
        "where",
    }
)

#: Patterns refused outright, each with the reason shown to the user. These are
#: a second line of defence: the allowlist already excludes them, and this
#: catches anything that slips past the parser.
REFUSED: tuple[tuple[re.Pattern[str], str], ...] = (
    # Structural first, so the reason given is the most specific one rather than
    # whichever pattern happens to appear earliest.
    (re.compile(r"[;&`]|\|\||&&"), "it chains commands, which could escape the allowlist"),
    (re.compile(r"-e(nc|ncoded)?c(ommand)?\b", re.I), "it hides the real command behind base64"),
    (re.compile(r"\$\(|\$\{|@\(", re.I), "it uses subexpressions, which can hide another command"),
    (re.compile(r"\bout-file\b|>\s*\S", re.I), "it writes to a file"),
    # Then specific dangerous capabilities.
    (re.compile(r"\b(iex|invoke-expression)\b", re.I), "it executes arbitrary text as code"),
    (
        re.compile(r"\b(downloadstring|downloadfile|invoke-webrequest|iwr|curl|wget)\b", re.I),
        "it fetches code or files from the internet",
    ),
    (re.compile(r"\b(add-type|new-object)\b", re.I), "it compiles or instantiates arbitrary types"),
    (
        re.compile(r"\[\s*(system\.)?(diagnostics|reflection|runtime|net)\b", re.I),
        "it reaches into .NET directly",
    ),
    (re.compile(r"\bstart-process\b", re.I), "it launches arbitrary programs"),
    (re.compile(r"\bset-executionpolicy\b", re.I), "it weakens PowerShell's script protections"),
    (re.compile(r"\bset-mppreference\b", re.I), "it changes Microsoft Defender settings"),
    (
        re.compile(r"\b(netsh|reg|sc|wmic|bcdedit|cmd|powershell|pwsh)\b", re.I),
        "it shells out to another command interpreter",
    ),
    # Finally the catch-all for any state-changing verb.
    (
        re.compile(r"\b(set|remove|disable|stop|clear|new|add)-\w+", re.I),
        "it changes system state, and this tool is read-only",
    ),
)

MAX_COMMAND_LENGTH = 400
DEFAULT_TIMEOUT = 30.0


class CommandRefused(ToolError):
    code = "jarvis.powershell.refused"
    http_status = 403


@dataclass(frozen=True)
class Validation:
    allowed: bool
    reason: str
    cmdlet: str = ""


def validate(command: str) -> Validation:
    """Decide whether a command may run. Default deny."""
    text = command.strip()
    if not text:
        return Validation(False, "The command was empty.")
    if len(text) > MAX_COMMAND_LENGTH:
        return Validation(False, f"The command is longer than {MAX_COMMAND_LENGTH} characters.")

    for pattern, reason in REFUSED:
        if pattern.search(text):
            return Validation(False, f"Jarvis will not run this because {reason}.")

    # Split on pipes: the first segment must be an allowed cmdlet, the rest must
    # be formatters that only shape output.
    segments = [s.strip() for s in text.split("|")]
    head = segments[0].split()
    if not head:
        return Validation(False, "No cmdlet was given.")

    cmdlet_name = head[0].lower()
    cmdlet = ALLOWED.get(cmdlet_name)
    if cmdlet is None:
        return Validation(
            False,
            f"{head[0]!r} is not on the list of commands Jarvis may run. "
            f"Only read-only system queries are allowed.",
        )

    for token in head[1:]:
        # Compare case-insensitively; PowerShell parameters are not case
        # sensitive and neither is the allowlist.
        if token.startswith("-") and token.lower() not in {p.lower() for p in cmdlet.parameters}:
            return Validation(
                False,
                f"{cmdlet.name} may not be given {token!r} here. "
                f"Allowed: {', '.join(sorted(cmdlet.parameters)) or 'no parameters'}.",
            )

    for segment in segments[1:]:
        words = segment.split()
        if not words:
            return Validation(False, "An empty step in the pipeline.")
        if words[0].lower() not in ALLOWED_FORMATTERS:
            return Validation(
                False,
                f"{words[0]!r} may not appear after a pipe. Only output "
                f"formatting is allowed there.",
            )

    return Validation(True, f"{cmdlet.name}: {cmdlet.description}", cmdlet.name)


class PowerShellTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        available = _on_windows()
        return ToolSpec(
            name="powershell",
            description=(
                "Run a read-only Windows system query from a fixed allowlist. "
                "Cannot change settings, download anything, or run arbitrary code."
            ),
            scopes=[Scope.SHELL_POWERSHELL],
            risk=Risk.MEDIUM,
            input_schema={
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string", "maxLength": MAX_COMMAND_LENGTH},
                    "timeout": {"type": "number", "default": DEFAULT_TIMEOUT},
                },
            },
            available=available,
            unavailable_reason=("" if available else "PowerShell is only available on Windows."),
        )

    @staticmethod
    def allowlist() -> list[dict[str, Any]]:
        return [
            {
                "cmdlet": c.name,
                "description": c.description,
                "parameters": sorted(c.parameters),
                "readOnly": c.read_only,
            }
            for c in sorted(ALLOWED.values(), key=lambda c: c.name)
        ]

    async def preview(self, args: dict[str, Any]) -> Preview:
        self.require(args, "command")
        command = str(args["command"])
        verdict = validate(command)
        if not verdict.allowed:
            return Preview(
                summary="Command refused",
                blocked=verdict.reason,
                reversible="undoable",
                blast_radius="Nothing changes.",
            )
        if not _on_windows():
            return Preview(
                summary="PowerShell is not available here",
                blocked="PowerShell only runs on Windows.",
                reversible="undoable",
                blast_radius="Nothing changes.",
            )
        return Preview(
            summary=f"Run {verdict.cmdlet}",
            targets=[command],
            affected=0,
            reversible="undoable",
            blast_radius=(
                f"Reads system state only — {verdict.reason} Nothing on this computer is modified."
            ),
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        self.require(args, "command")
        command = str(args["command"])
        verdict = validate(command)
        if not verdict.allowed:
            # Re-validated at the point of execution, never trusting the preview.
            raise CommandRefused(verdict.reason, command=command[:120])
        if not _on_windows():
            raise ToolError("PowerShell only runs on Windows.")

        result = run_command(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-NoLogo",
                "-Command",
                command,
            ],
            timeout=float(args.get("timeout") or DEFAULT_TIMEOUT),
        )
        log.info("powershell query", cmdlet=verdict.cmdlet, exit_code=result.exit_code)
        return ToolResult(
            ok=result.exit_code == 0,
            summary=(
                f"{verdict.cmdlet} returned {len(result.stdout.splitlines())} lines"
                if result.exit_code == 0
                else f"{verdict.cmdlet} failed with exit code {result.exit_code}"
            ),
            data={
                "cmdlet": verdict.cmdlet,
                "stdout": result.stdout,
                "exitCode": result.exit_code,
                "truncated": result.truncated,
            },
            error=result.stderr if result.exit_code != 0 else "",
        )

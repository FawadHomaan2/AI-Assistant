"""SecurityTool — report this machine's security posture.

Read-only, deliberately and permanently. There is no operation here that turns
Defender on, enables the firewall or encrypts a drive, and that is a design
decision rather than an unfinished one: an assistant that can change security
controls is a far more valuable thing to compromise than one that can only
describe them. Jarvis reports; the user acts, in Windows' own interface, where
Windows can ask for consent properly.

The two write operations — acknowledging a finding and trusting a baseline
entry — change what Jarvis *says*, never what the machine *does*.
"""

from __future__ import annotations

from typing import Any

from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.security.center import SecurityCenter
from jarvis.tools.base import Preview, Tool, ToolError, ToolInputInvalid, ToolResult, ToolSpec
from jarvis.util.logging import get_logger

log = get_logger(__name__)

OPERATIONS = ("scan", "status", "findings", "baseline", "acknowledge", "trust")

#: Operations that only read.
READ_ONLY = ("scan", "status", "findings", "baseline")


class SecurityTool(Tool):
    def __init__(self, center: SecurityCenter) -> None:
        self.center = center

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="security",
            description=(
                "Check antivirus, firewall, disk encryption, startup programs, "
                "network listeners and removable devices, and report what was "
                "found with the evidence behind it. Never changes a setting."
            ),
            scopes=[Scope.SECURITY_READ],
            risk=Risk.SAFE,
            input_schema={
                "type": "object",
                "required": ["operation"],
                "properties": {
                    "operation": {"type": "string", "enum": list(OPERATIONS)},
                    "id": {"type": "string", "description": "Finding id to acknowledge"},
                    "fingerprint": {"type": "string", "description": "Baseline entry to trust"},
                    "trusted": {"type": "boolean"},
                },
            },
        )

    def risk_for(self, args: dict[str, Any], preview: Preview) -> Risk:
        del preview
        operation = str(args.get("operation", ""))
        # Marking something trusted silences future reports about it. That is a
        # change to what Jarvis will tell you, so it is not free.
        return Risk.SAFE if operation in READ_ONLY else Risk.LOW

    def scopes_for(self, args: dict[str, Any]) -> list[Scope]:
        operation = str(args.get("operation", ""))
        if operation in READ_ONLY:
            return [Scope.SECURITY_READ]
        return [Scope.SECURITY_READ, Scope.SECURITY_MODIFY]

    async def preview(self, args: dict[str, Any]) -> Preview:
        operation = str(args.get("operation", "scan"))
        if operation not in OPERATIONS:
            raise ToolInputInvalid(
                f"Unknown operation {operation!r}. Supported: {', '.join(OPERATIONS)}."
            )

        if operation == "scan":
            return Preview(
                summary=f"Run {len(self.center.check_types)} security checks — reading only",
                affected=0,
                reversible="undoable",
                blast_radius=(
                    "Reads antivirus, firewall, encryption, startup, network and "
                    "device state. Nothing is changed; Jarvis cannot turn security "
                    "settings on or off."
                ),
            )
        if operation in READ_ONLY:
            return Preview(
                summary=f"Read security {operation}",
                affected=0,
                reversible="undoable",
                blast_radius="Nothing changes.",
            )
        if operation == "acknowledge":
            self.require(args, "id")
            return Preview(
                summary=f"Acknowledge finding {args['id']}",
                targets=[str(args["id"])],
                affected=1,
                reversible="undoable",
                blast_radius=(
                    "Marks this as seen so it stops being highlighted. The "
                    "condition itself is unchanged and will be reported again if "
                    "it gets worse."
                ),
            )

        self.require(args, "fingerprint")
        return Preview(
            summary=f"Trust {args['fingerprint']}",
            targets=[str(args["fingerprint"])],
            affected=1,
            reversible="undoable",
            blast_radius=(
                "Jarvis will stop reporting this. Only do that for something you "
                "recognise — trusting it does not make it safe, it makes Jarvis "
                "quiet about it."
            ),
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        operation = str(args.get("operation", "scan"))

        if operation == "scan":
            report = self.center.scan()
            return ToolResult(
                ok=True,
                summary=report.headline(),
                data=report.to_dict(),
            )

        if operation == "status":
            status = self.center.status()
            return ToolResult(
                ok=True,
                summary=(
                    f"{status['openFindings']} open finding(s); "
                    f"{status['baselineItems']} item(s) known to be normal"
                ),
                data=status,
            )

        if operation == "findings":
            findings = self.center.findings.open_findings()
            return ToolResult(
                ok=True,
                summary=f"{len(findings)} open finding(s)",
                data={"findings": findings},
            )

        if operation == "baseline":
            entries = self.center.baseline.entries()
            return ToolResult(
                ok=True,
                summary=f"{len(entries)} item(s) Jarvis considers normal here",
                data={"baseline": [e.to_dict() for e in entries]},
            )

        if operation == "acknowledge":
            self.require(args, "id")
            found = self.center.findings.acknowledge(str(args["id"]))
            if not found:
                raise ToolError(f"There is no finding with id {args['id']!r}.")
            return ToolResult(
                ok=True,
                summary="Marked as seen. The condition itself has not changed.",
                data={"id": args["id"]},
                changes=[f"Acknowledged {args['id']}"],
            )

        self.require(args, "fingerprint")
        trusted = bool(args.get("trusted", True))
        found = self.center.baseline.trust(str(args["fingerprint"]), trusted)
        if not found:
            raise ToolError(f"Nothing in the baseline matches {args['fingerprint']!r}.")
        return ToolResult(
            ok=True,
            summary=(
                "Jarvis will stop reporting this."
                if trusted
                else "Jarvis will report on this again."
            ),
            data={"fingerprint": args["fingerprint"], "trusted": trusted},
            changes=[f"{'Trusted' if trusted else 'Untrusted'} {args['fingerprint']}"],
        )

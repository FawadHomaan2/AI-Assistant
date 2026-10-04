"""NetworkTool — read-only diagnostics for *this* machine.

Deliberately read-only and deliberately local. There is no port scan, no host
discovery and no remote probe: an assistant that can enumerate other machines on
your network is a tool for attacking them, and this one exists to tell you about
your own computer. Connectivity is checked by resolving and connecting to a
single well-known host, nothing more.
"""

from __future__ import annotations

import socket
import time
from typing import Any

import psutil

from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.tools.base import Preview, Tool, ToolInputInvalid, ToolResult, ToolSpec
from jarvis.util.logging import get_logger

log = get_logger(__name__)

OPERATIONS = ("interfaces", "connections", "connectivity", "counters")

#: Used only to answer "is the internet reachable?". Cloudflare's resolver is
#: chosen because it is an IP, so a DNS failure is distinguishable from a
#: routing failure.
CONNECTIVITY_HOST = "1.1.1.1"
CONNECTIVITY_PORT = 443
DNS_PROBE = "cloudflare.com"


class NetworkTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="network",
            description=(
                "Report this computer's network interfaces, its own open "
                "connections, traffic counters, and whether the internet is "
                "reachable. Read-only, and only about this machine."
            ),
            scopes=[Scope.NET_READ],
            risk=Risk.SAFE,
            input_schema={
                "type": "object",
                "required": ["operation"],
                "properties": {
                    "operation": {"type": "string", "enum": list(OPERATIONS)},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        )

    async def preview(self, args: dict[str, Any]) -> Preview:
        operation = str(args.get("operation", "interfaces"))
        if operation not in OPERATIONS:
            raise ToolInputInvalid(
                f"Unknown operation {operation!r}. Supported: {', '.join(OPERATIONS)}."
            )
        return Preview(
            summary=f"Read network {operation} — inspection only",
            affected=0,
            reversible="undoable",
            blast_radius="Nothing changes. Jarvis does not probe other machines.",
        )

    @staticmethod
    def interfaces() -> list[dict[str, Any]]:
        stats = psutil.net_if_stats()
        out: list[dict[str, Any]] = []
        for name, addresses in psutil.net_if_addrs().items():
            entry: dict[str, Any] = {"name": name, "addresses": [], "up": False, "speedMbps": None}
            if stat := stats.get(name):
                entry["up"] = stat.isup
                entry["speedMbps"] = stat.speed or None
                entry["mtu"] = stat.mtu
            for address in addresses:
                family = {
                    socket.AF_INET: "ipv4",
                    socket.AF_INET6: "ipv6",
                    psutil.AF_LINK: "mac",
                }.get(address.family)
                if family:
                    entry["addresses"].append(
                        {"family": family, "address": address.address, "netmask": address.netmask}
                    )
            out.append(entry)
        return out

    @staticmethod
    def connections(limit: int = 50) -> list[dict[str, Any]]:
        """This machine's own sockets, with the process that owns each one."""
        names: dict[int, str] = {}
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                names[proc.info["pid"]] = proc.info["name"] or ""
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        out: list[dict[str, Any]] = []
        try:
            sockets = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, PermissionError):
            log.info("connection list needs elevation on this platform")
            return []

        for conn in sockets:
            if conn.status == psutil.CONN_NONE:
                continue
            out.append(
                {
                    "localAddress": f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else "",
                    "remoteAddress": f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else "",
                    "status": conn.status,
                    "pid": conn.pid,
                    "process": names.get(conn.pid or -1, ""),
                    "family": "ipv6" if conn.family == socket.AF_INET6 else "ipv4",
                }
            )
            if len(out) >= limit:
                break
        out.sort(key=lambda c: (not c["remoteAddress"], c["process"]))
        return out

    @staticmethod
    def counters() -> dict[str, Any]:
        total = psutil.net_io_counters()
        per_interface = {
            name: {"sentBytes": c.bytes_sent, "receivedBytes": c.bytes_recv}
            for name, c in psutil.net_io_counters(pernic=True).items()
        }
        return {
            "sentBytes": total.bytes_sent,
            "receivedBytes": total.bytes_recv,
            "packetsSent": total.packets_sent,
            "packetsReceived": total.packets_recv,
            "errorsIn": total.errin,
            "errorsOut": total.errout,
            "dropsIn": total.dropin,
            "dropsOut": total.dropout,
            "perInterface": per_interface,
        }

    @staticmethod
    def connectivity() -> dict[str, Any]:
        """Distinguish 'no network', 'no DNS' and 'working'.

        Three different problems with three different fixes, so reporting them
        as one "no internet" would be unhelpful.
        """
        result: dict[str, Any] = {"reachable": False, "dns": False, "latencyMs": None}

        started = time.monotonic()
        try:
            with socket.create_connection((CONNECTIVITY_HOST, CONNECTIVITY_PORT), timeout=4):
                result["reachable"] = True
                result["latencyMs"] = int((time.monotonic() - started) * 1000)
        except OSError as exc:
            result["detail"] = f"Could not reach {CONNECTIVITY_HOST}: {exc}"

        try:
            socket.getaddrinfo(DNS_PROBE, 443)
            result["dns"] = True
        except OSError as exc:
            result["dnsDetail"] = f"DNS lookup failed: {exc}"

        if result["reachable"] and result["dns"]:
            result["verdict"] = "Internet is reachable and name resolution works."
        elif result["reachable"]:
            result["verdict"] = (
                "The network is up but DNS is not resolving. Websites will fail "
                "to load even though the connection works."
            )
        elif result["dns"]:
            result["verdict"] = (
                "DNS resolves but the connection was refused — a firewall or "
                "proxy may be blocking outbound traffic."
            )
        else:
            result["verdict"] = "No internet connection could be established."
        return result

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        operation = str(args.get("operation", "interfaces"))
        limit = int(args.get("limit") or 50)

        if operation == "interfaces":
            interfaces = self.interfaces()
            up = [i["name"] for i in interfaces if i["up"]]
            return ToolResult(
                ok=True,
                summary=f"{len(interfaces)} interface(s), {len(up)} up: {', '.join(up[:4])}",
                data={"interfaces": interfaces},
            )
        if operation == "connections":
            sockets = self.connections(limit)
            external = [c for c in sockets if c["remoteAddress"]]
            return ToolResult(
                ok=True,
                summary=(
                    f"{len(sockets)} connection(s), {len(external)} to remote hosts"
                    if sockets
                    else "No connections could be read (this usually needs elevation)"
                ),
                data={"connections": sockets},
            )
        if operation == "counters":
            from jarvis.tools.systeminfo import human

            counters = self.counters()
            return ToolResult(
                ok=True,
                summary=f"Received {human(counters['receivedBytes'])}, "
                f"sent {human(counters['sentBytes'])} since boot",
                data=counters,
            )

        reachability = self.connectivity()
        return ToolResult(ok=True, summary=str(reachability["verdict"]), data=reachability)

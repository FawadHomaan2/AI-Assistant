"""Running a plugin in its own process.

Isolation here is a *process* boundary, not a security sandbox, and the
difference is stated plainly rather than implied. What this gives you:

  - **No ambient credentials.** The child's environment is built from nothing
    rather than inherited, so an API key in Jarvis's environment is not in the
    plugin's. This is the single most valuable property: most plugin incidents
    are a plugin reading something it was never handed.
  - **No shared memory.** A plugin cannot reach into the host's objects, the
    policy engine or the credential store. It can only send messages.
  - **Containment of crashes and hangs.** A plugin that segfaults, loops or
    blocks forever is killed on a timeout and reported, rather than taking the
    assistant with it.

What it does **not** give you, and what the interface says before you enable
anything: a plugin runs as you, with your file access and your network. A
malicious plugin can read your documents directly without asking Jarvis. The
scope system governs what it can do *through Jarvis*, which is a real and
useful boundary, and it is not a substitute for trusting the plugin's author.
Proper confinement needs an OS sandbox (AppContainer on Windows) and is noted
in docs/PHASES.md as not built.

The protocol is JSON lines over stdin/stdout: one request object in, one
response object out. Deliberately not pickle — unpickling data from a plugin
would execute whatever it sent.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: How long one plugin call may take before it is killed.
CALL_TIMEOUT_SECONDS = 20.0

#: How long a plugin gets to shut down politely before it is killed.
STOP_TIMEOUT_SECONDS = 3.0

#: A plugin returning 50 MB of JSON would be a denial of service against the
#: host, not a useful result.
MAX_RESPONSE_BYTES = 1_000_000

#: Variables the child genuinely needs. Everything else — API keys, tokens,
#: proxy credentials, the whole of the user's environment — is left behind.
ENV_ALLOWLIST = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL", "TZ")


class PluginError(JarvisError):
    code = "jarvis.plugin.failed"
    http_status = 500


class PluginTimeout(PluginError):
    code = "jarvis.plugin.timeout"
    http_status = 504


@dataclass
class PluginResponse:
    ok: bool
    data: dict[str, Any]
    error: str = ""


def child_environment() -> dict[str, str]:
    """The environment a plugin process gets: almost nothing.

    Built from an allowlist rather than by removing known-secret names. A
    denylist would leak anything nobody thought of, which over time is
    everything. `PYTHONPATH` is set by the caller to the plugin's own folder
    and nothing else, so a plugin imports from itself and the standard library.
    """
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    # Keeps the plugin's imports to its own folder and the standard library.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["JARVIS_PLUGIN"] = "1"
    return env


class PluginProcess:
    """One plugin, running as a child process."""

    def __init__(self, name: str, directory: Path, entry: str) -> None:
        self.name = name
        self.directory = directory
        self.entry = entry
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        if self.running:
            return
        if (
            not (self.directory / f"{self.entry.split('.')[0]}.py").exists()
            and not (self.directory / self.entry.replace(".", os.sep)).exists()
        ):
            raise PluginError(
                f"{self.name} says its entry point is {self.entry!r}, but there is no "
                f"such module in {self.directory}."
            )
        try:
            # `-s` keeps the user's site-packages out. `-I` would be stronger
            # but also strips the plugin's own folder from the import path,
            # so the plugin cannot import itself. The environment is built
            # from an allowlist instead, which is where the real isolation is.
            env = child_environment()
            env["PYTHONPATH"] = str(self.directory)
            self._process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-s",
                "-m",
                self.entry,
                cwd=str(self.directory),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # The stream reader's own buffer, which otherwise defaults to
                # 64 KiB and rejects a large reply with an asyncio message
                # about separators that means nothing to a user.
                limit=MAX_RESPONSE_BYTES,
            )
        except OSError as exc:
            raise PluginError(f"Could not start {self.name}: {exc}") from exc
        log.info("plugin started", plugin=self.name, pid=self._process.pid)

    async def call(self, tool: str, args: dict[str, Any]) -> PluginResponse:
        """Send one request and wait for one response."""
        async with self._lock:
            await self.start()
            process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                raise PluginError(f"{self.name} is not running.")

            request = json.dumps({"tool": tool, "args": args}, default=str) + "\n"
            try:
                process.stdin.write(request.encode())
                await process.stdin.drain()
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=CALL_TIMEOUT_SECONDS
                )
            except TimeoutError as exc:
                await self.stop()
                raise PluginTimeout(
                    f"{self.name} did not answer within {CALL_TIMEOUT_SECONDS:.0f} seconds "
                    f"and has been stopped. Nothing it was doing was completed."
                ) from exc
            except ValueError as exc:
                # The reply was larger than the reader's buffer. asyncio raises
                # "Separator is not found, and chunk exceed the limit", which
                # is true and useless; the user gets the reason instead.
                await self.stop()
                raise PluginError(
                    f"{self.name} returned more than "
                    f"{MAX_RESPONSE_BYTES // 1000} KB in one reply, which Jarvis "
                    f"refuses to load."
                ) from exc
            except (BrokenPipeError, ConnectionResetError) as exc:
                stderr = await self._drain_stderr()
                await self.stop()
                raise PluginError(f"{self.name} stopped unexpectedly.{stderr}") from exc

            if not line:
                stderr = await self._drain_stderr()
                await self.stop()
                raise PluginError(f"{self.name} closed without answering.{stderr}")
            if len(line) > MAX_RESPONSE_BYTES:
                await self.stop()
                raise PluginError(
                    f"{self.name} returned more than {MAX_RESPONSE_BYTES // 1000} KB "
                    f"in one reply, which Jarvis refuses to load."
                )

            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PluginError(f"{self.name} returned something that is not JSON.") from exc
            if not isinstance(payload, dict):
                raise PluginError(f"{self.name} returned {type(payload).__name__}, not an object.")

            raw_data = payload.get("data")
            data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            return PluginResponse(
                ok=bool(payload.get("ok", False)),
                data=data,
                error=str(payload.get("error", "")),
            )

    async def _drain_stderr(self) -> str:
        """The plugin's own error output, so a crash is explained not just reported."""
        if self._process is None or self._process.stderr is None:
            return ""
        try:
            data = await asyncio.wait_for(self._process.stderr.read(4096), timeout=1.0)
        except (TimeoutError, OSError):
            return ""
        text = data.decode("utf-8", errors="replace").strip()
        return f" It said: {text.splitlines()[-1]}" if text else ""

    async def stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT_SECONDS)
        except (TimeoutError, ProcessLookupError, OSError):
            # It ignored the polite request. A plugin does not get to refuse.
            with_kill = getattr(process, "kill", None)
            if with_kill is not None:
                try:
                    process.kill()
                    await process.wait()
                except (ProcessLookupError, OSError):
                    pass
        log.info("plugin stopped", plugin=self.name)

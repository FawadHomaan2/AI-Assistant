"""The one place Jarvis spawns a process.

Rules enforced here rather than trusted to call sites:
  * argument **lists** only, and the shell is never invoked
  * a timeout is mandatory
  * output is size-capped, so a runaway command cannot exhaust memory
  * the environment is scrubbed of anything that could redirect execution
  * every child is registered so the emergency stop can terminate it
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass

from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger

log = get_logger(__name__)

MAX_OUTPUT_BYTES = 2 * 1024 * 1024

#: Variables that can change which binary runs or how it behaves.
_SCRUBBED = (
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "PYTHONSTARTUP",
    "PYTHONPATH",
    "PSModulePath",
)

_children: set[int] = set()
_lock = threading.Lock()


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = False
    timed_out: bool = False


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in _SCRUBBED:
        env.pop(key, None)
    return env


def running_children() -> list[int]:
    with _lock:
        return sorted(_children)


def terminate_all() -> int:
    """Kill every child Jarvis started. Used by the emergency stop."""
    import signal

    killed = 0
    with _lock:
        pids = list(_children)
        _children.clear()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except OSError:
            continue
    if killed:
        log.warning("terminated child processes", count=killed)
    return killed


def run_command(
    argv: list[str],
    *,
    timeout: float,
    cwd: str | None = None,
) -> CommandResult:
    """Run a command from an argument list.

    `argv` is a list by type, so there is no code path where a string becomes a
    shell command — which is the whole point.
    """
    if not argv or not argv[0]:
        raise JarvisError("No command was given.")
    if timeout <= 0:
        raise JarvisError("A command must have a positive timeout.")

    log.info("running command", program=argv[0], argument_count=len(argv) - 1)
    try:
        process = subprocess.Popen(  # noqa: S603 - argv list; shell is never used
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=_environment(),
            text=True,
            errors="replace",
            shell=False,
        )
    except FileNotFoundError as exc:
        raise JarvisError(f"{argv[0]} is not installed or not on the PATH.") from exc
    except OSError as exc:
        raise JarvisError(f"Could not run {argv[0]}: {exc}") from exc

    with _lock:
        _children.add(process.pid)

    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate()
    finally:
        with _lock:
            _children.discard(process.pid)

    truncated = len(stdout) > MAX_OUTPUT_BYTES
    if truncated:
        stdout = stdout[:MAX_OUTPUT_BYTES] + "\n[output truncated]"

    return CommandResult(
        exit_code=process.returncode if not timed_out else 124,
        stdout=stdout,
        stderr=stderr[:16384],
        truncated=truncated,
        timed_out=timed_out,
    )

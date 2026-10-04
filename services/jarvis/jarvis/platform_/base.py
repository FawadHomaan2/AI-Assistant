"""OS adapter interfaces.

Three backends, selected once at startup. Each has a Windows implementation and
a POSIX one. Where POSIX genuinely cannot do something — managing another
application's windows — the method raises `PlatformUnsupported` rather than
returning an empty result that would read as "there are no windows".
"""

from __future__ import annotations

import abc

from jarvis.platform_.types import AppInfo, LaunchResult, ProcessInfo, WindowInfo


class ProcessBackend(abc.ABC):
    @abc.abstractmethod
    def list_all(self, *, limit: int = 500) -> list[ProcessInfo]: ...

    @abc.abstractmethod
    def get(self, pid: int) -> ProcessInfo | None: ...

    @abc.abstractmethod
    def find(self, name: str) -> list[ProcessInfo]: ...

    @abc.abstractmethod
    def terminate(self, pid: int, *, force: bool = False, timeout: float = 5.0) -> bool:
        """Ask a process to exit; `force` kills it. Returns True once it is gone."""


class WindowBackend(abc.ABC):
    @abc.abstractmethod
    def list_all(self, *, visible_only: bool = True) -> list[WindowInfo]: ...

    @abc.abstractmethod
    def foreground(self) -> WindowInfo | None: ...

    @abc.abstractmethod
    def focus(self, handle: int) -> bool: ...

    @abc.abstractmethod
    def minimise(self, handle: int) -> bool: ...

    @abc.abstractmethod
    def maximise(self, handle: int) -> bool: ...

    @abc.abstractmethod
    def restore(self, handle: int) -> bool: ...

    @abc.abstractmethod
    def close(self, handle: int) -> bool:
        """Ask the window to close, the way clicking its X does.

        Deliberately a request, not a kill: the application gets to prompt about
        unsaved work. Force-terminating is a separate, higher-risk operation on
        the process backend.
        """


class AppBackend(abc.ABC):
    @abc.abstractmethod
    def catalogue(self, *, refresh: bool = False) -> list[AppInfo]:
        """Applications installed on this machine."""

    @abc.abstractmethod
    def launch(self, app: AppInfo, *, arguments: list[str] | None = None) -> LaunchResult: ...

    @abc.abstractmethod
    def describe(self) -> str:
        """One line naming the mechanism, for the activity log."""


class Backends:
    """The three adapters, bundled."""

    def __init__(
        self, processes: ProcessBackend, windows: WindowBackend, apps: AppBackend, platform: str
    ) -> None:
        self.processes = processes
        self.windows = windows
        self.apps = apps
        self.platform = platform

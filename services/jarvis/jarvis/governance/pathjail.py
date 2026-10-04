"""Path jail.

Every filesystem path the agent proposes passes through here before any tool
touches it. The jail answers one question: *is this exact location, after full
resolution, inside somewhere the user has allowed and outside everywhere that is
permanently forbidden?*

Resolution order matters. A check performed on the string the model produced is
worthless, because `C:\\Users\\me\\Desktop\\..\\..\\..\\Windows\\System32` and a
symlink named `Desktop\\notes` pointing at `C:\\Windows` both look fine as text.
So the jail canonicalises first and compares second, never the other way round.

Known limitation, stated rather than hidden: between resolution and the actual
syscall there is a window in which a component could be swapped for a symlink
(TOCTOU). Closing it properly needs handle-based operations (`O_NOFOLLOW`,
`NtCreateFile` with `FILE_OPEN_REPARSE_POINT`). For a single-user assistant the
realistic attacker is a confused model or a malicious document, not a local race,
so the jail is the right control here — but it is not a defence against another
process actively racing it.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePath, PureWindowsPath

from jarvis.config import folders, paths
from jarvis.util.errors import JarvisError


class PathDenied(JarvisError):
    """A path was refused. `reason` says which rule fired, for the UI to show."""

    code = "jarvis.path.denied"
    http_status = 403

    def __init__(self, message: str, *, reason: str, path: str = "", **context: object) -> None:
        super().__init__(message, reason=reason, path=path, **context)
        self.reason = reason


class Access(StrEnum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"


#: Windows reserves these names in every directory, with or without an
#: extension. Opening one talks to a device, not a file.
_DEVICE_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)

#: Path shapes that are never legitimate input from an agent.
_DEVICE_PREFIXES = ("\\\\.\\", "\\\\?\\globalroot", "//./")

#: Directories no grant can open, on any platform. Checked before the allowlist,
#: so "allow C:\" would still not open System32.
_WINDOWS_FORBIDDEN = (
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData\Microsoft\Windows\Start Menu",
    r"C:\$Recycle.Bin",
    r"C:\System Volume Information",
    r"C:\Boot",
    r"C:\Recovery",
)

_POSIX_FORBIDDEN = (
    "/etc",
    "/bin",
    "/sbin",
    "/usr",
    "/boot",
    "/sys",
    "/proc",
    "/dev",
    "/var/log",
    "/root/.ssh",
)

#: Filenames that are credentials wherever they live, so reading them is refused
#: even inside an allowed folder.
_SECRET_FILENAMES = frozenset(
    {
        ".env",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "credentials",
        "ntuser.dat",
        "sam",
        "security",
    }
)

_SECRET_SUFFIXES = (".pem", ".pfx", ".p12", ".key", ".keystore", ".jks", ".ppk")

#: Directories whose contents are credential stores or private application data.
_SECRET_DIR_PARTS = frozenset({".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", "appdata"})

_TRAILING_JUNK = re.compile(r"[. ]+$")


@dataclass(frozen=True)
class ResolvedPath:
    """A path that passed the jail. Tools use `.path` and nothing else."""

    path: Path
    root: Path
    access: Access
    existed: bool

    def __str__(self) -> str:
        return str(self.path)


def _is_windows_style(raw: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", raw)) or raw.startswith("\\\\")


def _reject_obvious_abuse(raw: str) -> None:
    if not raw or not raw.strip():
        raise PathDenied("No path was given.", reason="empty")
    if "\x00" in raw:
        raise PathDenied("Path contains a null byte.", reason="null_byte", path=raw[:80])

    lowered = raw.lower().replace("/", "\\")
    for prefix in _DEVICE_PREFIXES:
        if lowered.startswith(prefix.lower().replace("/", "\\")):
            raise PathDenied(
                "That is a device path, not a file.", reason="device_path", path=raw[:120]
            )

    # UNC paths reach other machines; this assistant acts on this computer only.
    if lowered.startswith("\\\\") and not lowered.startswith("\\\\?\\"):
        raise PathDenied(
            "Network paths are not allowed — Jarvis acts on this computer only.",
            reason="unc_path",
            path=raw[:120],
        )

    parts = [p for p in re.split(r"[\\/]+", raw) if p]
    for part in parts:
        # A component of three or more dots is never a real folder, and is a
        # long-standing Windows traversal trick ("....//" collapsing to "../").
        if re.fullmatch(r"\.{3,}", part):
            raise PathDenied(
                f"{part!r} is not a valid folder name.",
                reason="dot_run",
                path=raw[:120],
            )
        # Windows silently strips trailing dots and spaces, so "System32." and
        # "System32" open the same directory while comparing as different
        # strings. Refusing the input removes that divergence entirely.
        if part != part.rstrip(". ") and part not in {".", ".."}:
            raise PathDenied(
                f"{part!r} ends in a dot or space, which Windows strips. Use the name without it.",
                reason="trailing_junk",
                path=raw[:120],
            )
        stem = part.split(".")[0].strip().lower()
        if stem in _DEVICE_NAMES:
            raise PathDenied(
                f"{part!r} is a reserved Windows device name.",
                reason="device_name",
                path=raw[:120],
            )
        # An alternate data stream hides content behind a legitimate filename.
        # A drive letter ("C:") is the one legal colon and never appears here.
        if ":" in part and not re.fullmatch(r"[A-Za-z]:", part):
            raise PathDenied(
                "Alternate data streams are not supported.",
                reason="ads",
                path=raw[:120],
            )


def _expand_token(raw: str) -> str:
    """Turn a leading known-folder token into a real path.

    Only the fixed table in `config.folders` expands. Arbitrary `%VAR%` and
    `$VAR` are left as literal text, so model output cannot reach a folder by
    naming an environment variable.
    """
    text = raw.strip().strip('"').strip("'")
    if text.startswith("~"):
        rest = text[1:].lstrip("\\/")
        return str(folders.folders()["home"] / rest) if rest else str(folders.folders()["home"])

    parts = re.split(r"[\\/]+", text, maxsplit=1)
    head = parts[0].strip().lower()
    if (resolved := folders.resolve_token(head)) is not None:
        return str(resolved / parts[1]) if len(parts) > 1 else str(resolved)
    return text


def _canonical(path: Path) -> Path:
    """Fully resolve, including symlinks, without requiring the path to exist.

    `Path.resolve(strict=False)` resolves the parts that do exist, which is what
    we need: creating `Desktop/new.txt` must be judged by where `Desktop` really
    points, not by the string.
    """
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        # A resolution loop or an unreadable component. Treat as unusable.
        raise PathDenied(
            "That path could not be resolved.", reason="unresolvable", path=str(path)[:120]
        ) from None


def _normalise_for_compare(path: Path) -> str:
    """Case-fold and strip Windows trailing junk so comparisons cannot be fooled.

    `System32.` and `System32 ` open the same directory on Windows but are
    different strings, and the filesystem is case-insensitive.
    """
    text = str(path)
    if sys.platform == "win32" or _is_windows_style(text):
        text = text.replace("/", "\\")
        text = "\\".join(_TRAILING_JUNK.sub("", part) for part in text.split("\\"))
        return text.casefold()
    return text


def _is_within(child: Path, parent: Path) -> bool:
    c, p = _normalise_for_compare(child), _normalise_for_compare(parent)
    if c == p:
        return True
    sep = "\\" if "\\" in p else "/"
    return c.startswith(p.rstrip(sep) + sep)


def _forbidden_roots() -> list[Path]:
    """Locations no grant can override."""
    roots: list[Path] = []
    source = _WINDOWS_FORBIDDEN if sys.platform == "win32" else _POSIX_FORBIDDEN
    roots.extend(Path(p) for p in source)
    # Jarvis must not be able to rewrite its own audit log, database or binary.
    roots.append(paths.data_dir())
    roots.append(paths.config_dir())
    roots.append(Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd())
    return roots


def _looks_like_a_secret(path: Path) -> str | None:
    """Flag credential files, which stay unreadable even inside an allowed root."""
    name = path.name.casefold()
    if name in _SECRET_FILENAMES:
        return f"{path.name} is a credential file"
    if any(name.endswith(suffix) for suffix in _SECRET_SUFFIXES):
        return f"{path.name} looks like a private key"
    parts = {p.casefold() for p in path.parts}
    if overlap := parts & _SECRET_DIR_PARTS:
        return f"it is inside {sorted(overlap)[0]!r}, which holds credentials"
    return None


class PathJail:
    """Decides whether a path may be touched, and how."""

    def __init__(
        self,
        allowed_roots: list[Path] | None = None,
        *,
        extra_forbidden: list[Path] | None = None,
    ) -> None:
        self.allowed_roots = [
            _canonical(Path(r)) for r in (allowed_roots or folders.default_allowed_roots())
        ]
        self.forbidden = [*_forbidden_roots(), *(extra_forbidden or [])]

    def describe(self) -> dict[str, list[str]]:
        return {
            "allowed": [str(p) for p in self.allowed_roots],
            "forbidden": [str(p) for p in self.forbidden],
        }

    def check(self, raw: str | Path, access: Access = Access.READ) -> ResolvedPath:
        """Resolve and authorise a path, or raise `PathDenied` explaining why."""
        text = str(raw)
        _reject_obvious_abuse(text)
        expanded = _expand_token(text)

        candidate = Path(expanded)
        if not candidate.is_absolute() and not _is_windows_style(expanded):
            raise PathDenied(
                f"{text!r} is a relative path. Jarvis needs a full path, or a folder "
                f"name like 'Desktop'.",
                reason="relative",
                path=text[:120],
            )

        resolved = _canonical(candidate)

        # Deny-list first: it wins over any grant, by design.
        for forbidden in self.forbidden:
            if _is_within(resolved, forbidden):
                raise PathDenied(
                    f"{resolved} is in a protected location ({forbidden}) that Jarvis "
                    f"never touches, whatever folders you have allowed.",
                    reason="forbidden_root",
                    path=str(resolved),
                )

        root = next((r for r in self.allowed_roots if _is_within(resolved, r)), None)
        if root is None:
            allowed = ", ".join(str(r) for r in self.allowed_roots) or "nothing"
            raise PathDenied(
                f"{resolved} is outside the folders Jarvis may use. Allowed: {allowed}. "
                f"You can grant more in Privacy settings.",
                reason="outside_allowed",
                path=str(resolved),
            )

        if (secret := _looks_like_a_secret(resolved)) is not None:
            raise PathDenied(
                f"Jarvis will not open {resolved.name}, because {secret}. "
                f"Credential files are refused even inside allowed folders.",
                reason="secret_file",
                path=str(resolved),
            )

        return ResolvedPath(path=resolved, root=root, access=access, existed=resolved.exists())

    def check_many(
        self, raws: list[str | Path], access: Access = Access.READ
    ) -> list[ResolvedPath]:
        return [self.check(r, access) for r in raws]

    def is_allowed(self, raw: str | Path, access: Access = Access.READ) -> bool:
        try:
            self.check(raw, access)
            return True
        except PathDenied:
            return False


def windows_path_preview(raw: str) -> str:
    """Render a path the way Windows would, for messages shown on Linux dev."""
    return str(PureWindowsPath(raw)) if _is_windows_style(raw) else str(PurePath(raw))


__all__ = ["Access", "PathDenied", "PathJail", "ResolvedPath", "windows_path_preview"]

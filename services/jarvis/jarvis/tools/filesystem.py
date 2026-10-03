"""FileSystemTool — the first tool that can change your computer.

One tool with an `operation` discriminator rather than fifteen tools, because
the permission story is shared: every operation resolves its paths through the
same jail, and risk varies by operation rather than by tool.

Risk assignment follows ARCHITECTURE §7:
  list/search/read/stat   SAFE     — inspection only
  create_folder/write_new LOW      — additive, nothing is lost
  move/rename/copy        MEDIUM   — reversible but disruptive
  delete (recycle bin)    MEDIUM   — recoverable
  overwrite               HIGH     — destroys existing content
  delete_permanently      CRITICAL — unrecoverable
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.governance.pathjail import Access, PathJail
from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.tools.base import Preview, Tool, ToolError, ToolInputInvalid, ToolResult, ToolSpec
from jarvis.tools.trash import send_to_trash, trash_available
from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: Reading is capped so a huge file cannot exhaust memory or flood a prompt.
MAX_READ_BYTES = 5 * 1024 * 1024
#: Directory walks are capped so a mistaken glob cannot hang the assistant.
MAX_WALK_ENTRIES = 50_000
MAX_RESULTS = 1_000

OPERATIONS = (
    "list",
    "search",
    "read",
    "stat",
    "create_folder",
    "write",
    "append",
    "move",
    "rename",
    "copy",
    "delete",
    "delete_permanently",
    "find_duplicates",
)

_RISK: dict[str, Risk] = {
    "list": Risk.SAFE,
    "search": Risk.SAFE,
    "read": Risk.SAFE,
    "stat": Risk.SAFE,
    "find_duplicates": Risk.SAFE,
    "create_folder": Risk.LOW,
    "write": Risk.LOW,  # raised to HIGH when it would overwrite
    "append": Risk.LOW,
    "copy": Risk.MEDIUM,
    "move": Risk.MEDIUM,
    "rename": Risk.MEDIUM,
    "delete": Risk.MEDIUM,  # Recycle Bin: recoverable
    "delete_permanently": Risk.CRITICAL,
}

_SCOPES: dict[str, list[Scope]] = {
    "list": [Scope.FS_READ],
    "search": [Scope.FS_READ],
    "read": [Scope.FS_READ],
    "stat": [Scope.FS_READ],
    "find_duplicates": [Scope.FS_READ],
    "create_folder": [Scope.FS_WRITE],
    "write": [Scope.FS_WRITE],
    "append": [Scope.FS_WRITE],
    "copy": [Scope.FS_WRITE],
    "move": [Scope.FS_WRITE],
    "rename": [Scope.FS_WRITE],
    "delete": [Scope.FS_DELETE],
    "delete_permanently": [Scope.FS_DELETE],
}


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" or size >= 10 else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")


@dataclass
class Entry:
    path: Path
    is_dir: bool
    size: int
    modified: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.path.name,
            "isDir": self.is_dir,
            "size": self.size,
            "sizeHuman": _human_size(self.size),
            "modified": _iso(self.modified),
        }


class FileSystemTool(Tool):
    def __init__(self, jail: PathJail) -> None:
        self.jail = jail

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="filesystem",
            description=(
                "Inspect and change files and folders inside the folders you have "
                "allowed. Supports listing, searching, reading, creating, writing, "
                "copying, moving, renaming, deleting (to the Recycle Bin) and "
                "finding duplicates."
            ),
            scopes=[Scope.FS_READ],
            risk=Risk.SAFE,
            input_schema={
                "type": "object",
                "required": ["operation"],
                "properties": {
                    "operation": {"type": "string", "enum": list(OPERATIONS)},
                    "path": {"type": "string", "description": "Target path or folder name"},
                    "destination": {"type": "string", "description": "For move/copy/rename"},
                    "content": {"type": "string", "description": "For write/append"},
                    "pattern": {"type": "string", "description": "Glob for search, e.g. *.pdf"},
                    "recursive": {"type": "boolean", "default": False},
                    "modified_within_days": {"type": "integer"},
                    "overwrite": {"type": "boolean", "default": False},
                },
            },
            output_schema={"type": "object"},
        )

    # ── risk / scopes vary by operation ──────────────────────────────────
    def risk_for(self, args: dict[str, Any], preview: Preview) -> Risk:
        op = str(args.get("operation", ""))
        risk = _RISK.get(op, Risk.MEDIUM)
        # Writing over an existing file destroys content; creating one does not.
        if op in ("write", "copy", "move", "rename") and preview.blast_radius.startswith(
            "Overwrites"
        ):
            return max(risk, Risk.HIGH)
        return risk

    def scopes_for(self, args: dict[str, Any]) -> list[Scope]:
        op = str(args.get("operation", ""))
        scopes = list(_SCOPES.get(op, [Scope.FS_READ]))
        # Moving out of a folder removes it from the source as well as writing
        # it to the destination.
        if op == "move":
            scopes.append(Scope.FS_READ)
        return scopes

    # ── helpers ──────────────────────────────────────────────────────────
    def _resolve(self, args: dict[str, Any], key: str, access: Access) -> Path:
        value = args.get(key)
        if not value:
            raise ToolInputInvalid(f"This operation needs a {key!r}.")
        return self.jail.check(str(value), access).path

    def _walk(self, root: Path, *, recursive: bool, pattern: str | None) -> list[Entry]:
        """Collect entries, capped, skipping anything the jail would refuse."""
        entries: list[Entry] = []
        seen = 0
        iterator = root.rglob(pattern or "*") if recursive else root.glob(pattern or "*")
        for item in iterator:
            seen += 1
            if seen > MAX_WALK_ENTRIES:
                log.warning("walk truncated", root=str(root), limit=MAX_WALK_ENTRIES)
                break
            # A symlink inside an allowed folder can still point out of it.
            if not self.jail.is_allowed(item):
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            entries.append(Entry(item, item.is_dir(), stat.st_size, stat.st_mtime))
            if len(entries) >= MAX_RESULTS:
                break
        return entries

    @staticmethod
    def _digest(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(block)
        return hasher.hexdigest()

    async def observe(self, args: dict[str, Any]) -> dict[str, Any]:
        """Stat the target, so the executor can verify the claimed change."""
        path = args.get("path")
        if not path:
            return {}
        try:
            resolved = self.jail.check(str(path)).path
        except Exception:  # an unresolvable path is simply unobservable
            return {}
        if not resolved.exists():
            return {"exists": False}
        try:
            stat = resolved.stat()
        except OSError:
            return {}
        return {
            "exists": True,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "isDir": resolved.is_dir(),
        }

    # ── preview ──────────────────────────────────────────────────────────
    async def preview(self, args: dict[str, Any]) -> Preview:
        """Measure what would happen. No side effects, so the prompt is honest."""
        op = str(args.get("operation", ""))
        if op not in OPERATIONS:
            raise ToolInputInvalid(f"Unknown operation {op!r}. Supported: {', '.join(OPERATIONS)}.")

        if op in ("list", "search", "stat", "read", "find_duplicates"):
            # The target is resolved even though nothing changes, because the
            # permission engine checks folder-limited grants against it. Without
            # it, `fs.read` granted on Desktop alone would permit reading
            # Documents — the grant would be decorative.
            target = self._resolve(args, "path", Access.READ)
            return Preview(
                summary=f"{op} {target} — inspection only, nothing changes",
                targets=[str(target)],
                affected=0,
                reversible="undoable",
                blast_radius="Nothing changes.",
            )

        if op == "create_folder":
            target = self._resolve(args, "path", Access.WRITE)
            if target.exists():
                return Preview(
                    summary=f"{target} already exists",
                    targets=[str(target)],
                    affected=0,
                    reversible="undoable",
                    blast_radius="Nothing changes.",
                    blocked=f"{target.name} already exists.",
                )
            return Preview(
                summary=f"Create folder {target.name}",
                targets=[str(target)],
                affected=1,
                reversible="undoable",
                blast_radius=f"Adds one empty folder inside {target.parent}.",
            )

        if op in ("write", "append"):
            target = self._resolve(args, "path", Access.WRITE)
            exists = target.exists()
            content = str(args.get("content", ""))
            if exists and op == "write" and not args.get("overwrite"):
                return Preview(
                    summary=f"{target.name} already exists",
                    targets=[str(target)],
                    affected=1,
                    reversible="permanent",
                    blast_radius=(
                        f"Overwrites {target.name} ({_human_size(target.stat().st_size)})."
                    ),
                    blocked=(
                        f"{target.name} already exists. Pass overwrite=true to replace it — "
                        f"its current contents would be lost."
                    ),
                )
            verb = "Append to" if op == "append" else ("Overwrite" if exists else "Create")
            return Preview(
                summary=f"{verb} {target.name} ({_human_size(len(content.encode()))})",
                targets=[str(target)],
                affected=1,
                reversible="permanent" if exists else "undoable",
                blast_radius=(
                    f"Overwrites {target.name}, losing its current contents."
                    if exists and op == "write"
                    else f"Adds content to {target.name}."
                ),
            )

        if op in ("move", "rename", "copy"):
            source = self._resolve(args, "path", Access.READ)
            dest = self._resolve(args, "destination", Access.WRITE)
            if not source.exists():
                return Preview(
                    summary=f"{source} does not exist",
                    affected=0,
                    blocked=f"{source} does not exist.",
                    reversible="undoable",
                    blast_radius="Nothing changes.",
                )
            # Moving into an existing directory means "put it inside".
            final = dest / source.name if dest.is_dir() else dest
            overwrites = final.exists()
            count = 1
            if source.is_dir():
                count = max(1, len(self._walk(source, recursive=True, pattern=None)))
            return Preview(
                summary=f"{op.capitalize()} {source.name} to {final}",
                targets=[f"{source} → {final}"],
                affected=count,
                reversible="undoable" if not overwrites else "permanent",
                blast_radius=(
                    f"Overwrites {final.name}, losing its current contents."
                    if overwrites
                    else f"{count} item{'s' if count != 1 else ''} "
                    f"{'copied' if op == 'copy' else 'moved'} within allowed folders."
                ),
            )

        if op in ("delete", "delete_permanently"):
            target = self._resolve(args, "path", Access.DELETE)
            if not target.exists():
                return Preview(
                    summary=f"{target} does not exist",
                    affected=0,
                    blocked=f"{target} does not exist, so there is nothing to delete.",
                    reversible="undoable",
                    blast_radius="Nothing changes.",
                )
            children = self._walk(target, recursive=True, pattern=None) if target.is_dir() else []
            count = len(children) + 1
            total = sum(c.size for c in children) + (
                0 if target.is_dir() else target.stat().st_size
            )
            recoverable = op == "delete"
            available, detail = trash_available()
            if recoverable and not available:
                return Preview(
                    summary=f"Cannot delete {target.name} recoverably",
                    targets=[str(target)],
                    affected=count,
                    reversible="unknown",
                    blast_radius="Nothing changes.",
                    blocked=(
                        f"A recoverable delete is not possible here: {detail}. "
                        f"Jarvis will not delete permanently instead."
                    ),
                )
            return Preview(
                summary=(
                    f"{'Permanently delete' if not recoverable else 'Move to the Recycle Bin'}: "
                    f"{target.name}"
                ),
                targets=[str(target), *[str(c.path) for c in children[:20]]],
                affected=count,
                reversible="recycle-bin" if recoverable else "permanent",
                blast_radius=(
                    f"{count} item{'s' if count != 1 else ''}, {_human_size(total)}"
                    + (
                        ". Recoverable from the Recycle Bin."
                        if recoverable
                        else ". This cannot be undone by Jarvis or by Windows."
                    )
                ),
            )

        raise ToolInputInvalid(f"Unsupported operation {op!r}.")

    # ── execute ──────────────────────────────────────────────────────────
    async def execute(self, args: dict[str, Any]) -> ToolResult:
        op = str(args.get("operation", ""))
        handler = getattr(self, f"_do_{op}", None)
        if handler is None:
            raise ToolInputInvalid(f"Unsupported operation {op!r}.")
        try:
            return await handler(args)  # type: ignore[no-any-return]
        except (OSError, shutil.Error) as exc:
            # Surface the real reason; never report success on a failed write.
            raise ToolError(f"{op} failed: {exc}", operation=op) from exc

    async def _do_list(self, args: dict[str, Any]) -> ToolResult:
        root = self._resolve(args, "path", Access.READ)
        if not root.is_dir():
            raise ToolError(f"{root} is not a folder.")
        entries = self._walk(root, recursive=bool(args.get("recursive")), pattern=None)
        entries.sort(key=lambda e: (not e.is_dir, e.path.name.lower()))
        return ToolResult(
            ok=True,
            summary=f"{len(entries)} item{'s' if len(entries) != 1 else ''} in {root.name}",
            data={"path": str(root), "entries": [e.to_dict() for e in entries]},
        )

    async def _do_search(self, args: dict[str, Any]) -> ToolResult:
        root = self._resolve(args, "path", Access.READ)
        pattern = str(args.get("pattern") or "*")
        entries = self._walk(root, recursive=True, pattern=pattern)
        if days := args.get("modified_within_days"):
            cutoff = datetime.now(tz=UTC).timestamp() - int(days) * 86400
            entries = [e for e in entries if e.modified >= cutoff]
        entries.sort(key=lambda e: e.modified, reverse=True)
        return ToolResult(
            ok=True,
            summary=f"{len(entries)} match{'es' if len(entries) != 1 else ''} for {pattern}",
            data={
                "pattern": pattern,
                "root": str(root),
                "matches": [e.to_dict() for e in entries],
                "truncated": len(entries) >= MAX_RESULTS,
            },
        )

    async def _do_stat(self, args: dict[str, Any]) -> ToolResult:
        target = self._resolve(args, "path", Access.READ)
        if not target.exists():
            raise ToolError(f"{target} does not exist.")
        stat = target.stat()
        return ToolResult(
            ok=True,
            summary=f"{target.name}: {_human_size(stat.st_size)}, modified {_iso(stat.st_mtime)}",
            data=Entry(target, target.is_dir(), stat.st_size, stat.st_mtime).to_dict(),
        )

    async def _do_read(self, args: dict[str, Any]) -> ToolResult:
        target = self._resolve(args, "path", Access.READ)
        if not target.is_file():
            raise ToolError(f"{target} is not a file.")
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            raise ToolError(
                f"{target.name} is {_human_size(size)}, over the "
                f"{_human_size(MAX_READ_BYTES)} read limit."
            )
        try:
            text = target.read_text(encoding="utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = target.read_text(encoding="utf-8", errors="replace")
            encoding = "utf-8 (with replacements)"
        return ToolResult(
            ok=True,
            summary=f"Read {target.name} ({_human_size(size)})",
            data={"path": str(target), "content": text, "encoding": encoding, "bytes": size},
        )

    async def _do_create_folder(self, args: dict[str, Any]) -> ToolResult:
        target = self._resolve(args, "path", Access.WRITE)
        target.mkdir(parents=True, exist_ok=False)
        return ToolResult(
            ok=True,
            summary=f"Created folder {target.name}",
            data={"path": str(target)},
            changes=[f"Created {target}"],
        )

    async def _do_write(self, args: dict[str, Any]) -> ToolResult:
        target = self._resolve(args, "path", Access.WRITE)
        content = str(args.get("content", ""))
        if target.exists() and not args.get("overwrite"):
            raise ToolError(f"{target.name} already exists. Pass overwrite=true to replace it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(
            ok=True,
            summary=f"Wrote {target.name} ({_human_size(len(content.encode()))})",
            data={"path": str(target), "bytes": len(content.encode())},
            changes=[f"Wrote {target}"],
        )

    async def _do_append(self, args: dict[str, Any]) -> ToolResult:
        target = self._resolve(args, "path", Access.WRITE)
        content = str(args.get("content", ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
        return ToolResult(
            ok=True,
            summary=f"Appended to {target.name}",
            data={"path": str(target)},
            changes=[f"Appended {len(content.encode())} bytes to {target}"],
        )

    async def _do_copy(self, args: dict[str, Any]) -> ToolResult:
        return await self._transfer(args, move=False)

    async def _do_move(self, args: dict[str, Any]) -> ToolResult:
        return await self._transfer(args, move=True)

    async def _do_rename(self, args: dict[str, Any]) -> ToolResult:
        return await self._transfer(args, move=True)

    async def _transfer(self, args: dict[str, Any], *, move: bool) -> ToolResult:
        source = self._resolve(args, "path", Access.READ)
        dest = self._resolve(args, "destination", Access.WRITE)
        if not source.exists():
            raise ToolError(f"{source} does not exist.")
        final = dest / source.name if dest.is_dir() else dest
        if final.exists() and not args.get("overwrite"):
            raise ToolError(f"{final} already exists. Pass overwrite=true to replace it.")
        final.parent.mkdir(parents=True, exist_ok=True)

        if move:
            shutil.move(str(source), str(final))
            verb = "Moved"
        elif source.is_dir():
            shutil.copytree(str(source), str(final), dirs_exist_ok=bool(args.get("overwrite")))
            verb = "Copied"
        else:
            shutil.copy2(str(source), str(final))
            verb = "Copied"

        return ToolResult(
            ok=True,
            summary=f"{verb} {source.name} to {final}",
            data={"source": str(source), "destination": str(final)},
            changes=[f"{verb} {source} → {final}"],
            undo_token=f"move:{final}:{source}" if move else "",
        )

    async def _do_delete(self, args: dict[str, Any]) -> ToolResult:
        target = self._resolve(args, "path", Access.DELETE)
        if not target.exists():
            raise ToolError(f"{target} does not exist.")
        result = send_to_trash(target)
        return ToolResult(
            ok=True,
            summary=f"Moved {target.name} to the Recycle Bin",
            data={"path": str(target), "method": result.method, "recoverable": True},
            changes=[f"Recycled {target}"],
        )

    async def _do_delete_permanently(self, args: dict[str, Any]) -> ToolResult:
        target = self._resolve(args, "path", Access.DELETE)
        if not target.exists():
            raise ToolError(f"{target} does not exist.")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        log.warning("permanent delete", path=str(target))
        return ToolResult(
            ok=True,
            summary=f"Permanently deleted {target.name}",
            data={"path": str(target), "recoverable": False},
            changes=[f"Permanently deleted {target}"],
        )

    async def _do_find_duplicates(self, args: dict[str, Any]) -> ToolResult:
        """Group identical files by content hash, not by name.

        Size is compared first so only genuine candidates are hashed — a folder
        of large distinct files costs almost nothing.
        """
        root = self._resolve(args, "path", Access.READ)
        entries = [e for e in self._walk(root, recursive=True, pattern=None) if not e.is_dir]

        by_size: dict[int, list[Entry]] = {}
        for entry in entries:
            if entry.size > 0:
                by_size.setdefault(entry.size, []).append(entry)

        groups: list[dict[str, Any]] = []
        wasted = 0
        for size, candidates in by_size.items():
            if len(candidates) < 2:
                continue
            by_hash: dict[str, list[Entry]] = {}
            for candidate in candidates:
                try:
                    by_hash.setdefault(self._digest(candidate.path), []).append(candidate)
                except OSError:
                    continue
            for digest, matches in by_hash.items():
                if len(matches) < 2:
                    continue
                matches.sort(key=lambda e: e.modified)
                wasted += size * (len(matches) - 1)
                groups.append(
                    {
                        "hash": digest[:16],
                        "size": size,
                        "sizeHuman": _human_size(size),
                        "keep": str(matches[0].path),
                        "duplicates": [str(m.path) for m in matches[1:]],
                    }
                )

        groups.sort(key=lambda g: int(g["size"]) * len(g["duplicates"]), reverse=True)
        return ToolResult(
            ok=True,
            summary=(
                f"{len(groups)} duplicate group{'s' if len(groups) != 1 else ''}, "
                f"{_human_size(wasted)} recoverable"
            ),
            data={"groups": groups, "wastedBytes": wasted, "scanned": len(entries)},
        )

    async def undo(self, token: str) -> ToolResult:
        """Reverse a move. Copies and deletes are not reversed here."""
        if not token.startswith("move:"):
            return await super().undo(token)
        _, moved_to, came_from = token.split(":", 2)
        current = self.jail.check(moved_to, Access.READ).path
        original = self.jail.check(came_from, Access.WRITE).path
        if not current.exists():
            raise ToolError(f"{current} is no longer there, so the move cannot be undone.")
        if original.exists():
            raise ToolError(f"{original} exists again, so undoing would overwrite it.")
        shutil.move(str(current), str(original))
        return ToolResult(
            ok=True,
            summary=f"Moved {original.name} back",
            changes=[f"Undid move: {current} → {original}"],
        )

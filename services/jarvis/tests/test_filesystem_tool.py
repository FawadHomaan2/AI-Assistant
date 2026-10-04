"""FileSystemTool: the first tool that can change the machine."""

from __future__ import annotations

import pytest

from jarvis.config import folders
from jarvis.governance.pathjail import PathDenied, PathJail
from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.tools.base import ToolError
from jarvis.tools.filesystem import FileSystemTool


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME_OVERRIDE", str(tmp_path))
    folders.reset_cache()
    desk = tmp_path / "Desktop"
    desk.mkdir()
    (tmp_path / "Documents").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "secret.txt").write_text("classified")
    yield desk
    folders.reset_cache()


@pytest.fixture
def tool(workspace):
    return FileSystemTool(PathJail([workspace, workspace.parent / "Documents"]))


class TestInspection:
    async def test_list(self, tool, workspace) -> None:
        (workspace / "a.txt").write_text("a")
        (workspace / "sub").mkdir()
        result = await tool.execute({"operation": "list", "path": str(workspace)})
        names = [e["name"] for e in result.data["entries"]]
        assert set(names) == {"a.txt", "sub"}
        assert result.data["entries"][0]["isDir"] is True  # folders first

    async def test_search_by_pattern(self, tool, workspace) -> None:
        for name in ("a.pdf", "b.pdf", "c.txt"):
            (workspace / name).write_text("x")
        result = await tool.execute(
            {"operation": "search", "path": str(workspace), "pattern": "*.pdf"}
        )
        assert len(result.data["matches"]) == 2

    async def test_search_by_age(self, tool, workspace) -> None:
        import os
        import time

        old = workspace / "old.txt"
        old.write_text("x")
        long_ago = time.time() - 90 * 86400
        os.utime(old, (long_ago, long_ago))
        (workspace / "new.txt").write_text("x")

        result = await tool.execute(
            {"operation": "search", "path": str(workspace), "modified_within_days": 31}
        )
        assert [m["name"] for m in result.data["matches"]] == ["new.txt"]

    async def test_read(self, tool, workspace) -> None:
        (workspace / "notes.txt").write_text("hello world")
        result = await tool.execute({"operation": "read", "path": str(workspace / "notes.txt")})
        assert result.data["content"] == "hello world"

    async def test_read_refuses_oversized_files(self, tool, workspace) -> None:
        from jarvis.tools.filesystem import MAX_READ_BYTES

        big = workspace / "big.bin"
        big.write_bytes(b"x" * (MAX_READ_BYTES + 10))
        with pytest.raises(ToolError, match="read limit"):
            await tool.execute({"operation": "read", "path": str(big)})

    async def test_inspection_is_tier_one(self, tool) -> None:
        preview = await tool.preview({"operation": "list", "path": "Desktop"})
        assert tool.risk_for({"operation": "list"}, preview) is Risk.SAFE
        assert preview.affected == 0


class TestMutation:
    async def test_create_folder(self, tool, workspace) -> None:
        await tool.execute({"operation": "create_folder", "path": str(workspace / "New")})
        assert (workspace / "New").is_dir()

    async def test_create_folder_preview_reports_an_existing_one(self, tool, workspace) -> None:
        (workspace / "Exists").mkdir()
        preview = await tool.preview(
            {"operation": "create_folder", "path": str(workspace / "Exists")}
        )
        assert preview.blocked
        assert preview.affected == 0

    async def test_write_creates(self, tool, workspace) -> None:
        await tool.execute(
            {"operation": "write", "path": str(workspace / "n.txt"), "content": "hi"}
        )
        assert (workspace / "n.txt").read_text() == "hi"

    # Overwriting destroys data; creating does not. The tiers differ.
    async def test_write_refuses_to_overwrite_without_being_told(self, tool, workspace) -> None:
        target = workspace / "n.txt"
        target.write_text("original")
        preview = await tool.preview({"operation": "write", "path": str(target), "content": "new"})
        assert preview.blocked
        assert "overwrite=true" in preview.blocked
        assert target.read_text() == "original"

    async def test_overwrite_is_high_risk(self, tool, workspace) -> None:
        target = workspace / "n.txt"
        target.write_text("original")
        args = {"operation": "write", "path": str(target), "content": "new", "overwrite": True}
        preview = await tool.preview(args)
        assert tool.risk_for(args, preview) is Risk.HIGH
        assert preview.reversible == "permanent"

    async def test_creating_a_new_file_is_low_risk(self, tool, workspace) -> None:
        args = {"operation": "write", "path": str(workspace / "fresh.txt"), "content": "x"}
        preview = await tool.preview(args)
        assert tool.risk_for(args, preview) is Risk.LOW
        assert preview.reversible == "undoable"

    async def test_move_and_undo(self, tool, workspace) -> None:
        source = workspace / "a.txt"
        source.write_text("x")
        dest = workspace / "sub"
        dest.mkdir()
        result = await tool.execute(
            {"operation": "move", "path": str(source), "destination": str(dest)}
        )
        assert not source.exists()
        assert (dest / "a.txt").exists()

        await tool.undo(result.undo_token)
        assert source.exists()
        assert not (dest / "a.txt").exists()

    async def test_undo_refuses_to_overwrite(self, tool, workspace) -> None:
        source = workspace / "a.txt"
        source.write_text("x")
        dest = workspace / "sub"
        dest.mkdir()
        result = await tool.execute(
            {"operation": "move", "path": str(source), "destination": str(dest)}
        )
        source.write_text("something new")
        with pytest.raises(ToolError, match="would overwrite"):
            await tool.undo(result.undo_token)

    async def test_copy_leaves_the_original(self, tool, workspace) -> None:
        source = workspace / "a.txt"
        source.write_text("x")
        await tool.execute(
            {"operation": "copy", "path": str(source), "destination": str(workspace / "b.txt")}
        )
        assert source.exists()
        assert (workspace / "b.txt").read_text() == "x"


class TestDeletion:
    async def test_delete_goes_to_the_recycle_bin(self, tool, workspace) -> None:
        target = workspace / "bye.txt"
        target.write_text("x")
        result = await tool.execute({"operation": "delete", "path": str(target)})
        assert not target.exists()
        assert result.data["recoverable"] is True

    async def test_delete_preview_says_it_is_recoverable(self, tool, workspace) -> None:
        target = workspace / "bye.txt"
        target.write_text("x")
        preview = await tool.preview({"operation": "delete", "path": str(target)})
        assert preview.reversible == "recycle-bin"
        assert "Recoverable" in preview.blast_radius

    async def test_permanent_delete_is_critical_and_says_so(self, tool, workspace) -> None:
        target = workspace / "gone.txt"
        target.write_text("x")
        args = {"operation": "delete_permanently", "path": str(target)}
        preview = await tool.preview(args)
        assert tool.risk_for(args, preview) is Risk.CRITICAL
        assert preview.reversible == "permanent"
        assert "cannot be undone" in preview.blast_radius

    async def test_folder_delete_counts_everything_inside(self, tool, workspace) -> None:
        folder = workspace / "stuff"
        folder.mkdir()
        for i in range(5):
            (folder / f"{i}.txt").write_text("x")
        preview = await tool.preview({"operation": "delete", "path": str(folder)})
        assert preview.affected == 6  # five files plus the folder

    async def test_deleting_something_absent_is_reported(self, tool, workspace) -> None:
        preview = await tool.preview({"operation": "delete", "path": str(workspace / "nope.txt")})
        assert preview.blocked
        assert "nothing to delete" in preview.blocked


class TestJailIsEnforced:
    """The tool must never be a way around the jail."""

    async def test_reading_outside_is_refused(self, tool, workspace) -> None:
        outside = workspace.parent / "private" / "secret.txt"
        with pytest.raises(PathDenied):
            await tool.execute({"operation": "read", "path": str(outside)})

    async def test_writing_outside_is_refused(self, tool, workspace) -> None:
        with pytest.raises(PathDenied):
            await tool.execute(
                {
                    "operation": "write",
                    "path": str(workspace.parent / "private" / "new.txt"),
                    "content": "x",
                }
            )

    async def test_moving_out_of_the_jail_is_refused(self, tool, workspace) -> None:
        source = workspace / "a.txt"
        source.write_text("x")
        with pytest.raises(PathDenied):
            await tool.execute(
                {
                    "operation": "move",
                    "path": str(source),
                    "destination": str(workspace.parent / "private"),
                }
            )
        assert source.exists(), "the source must be untouched when the destination is refused"

    async def test_listing_skips_entries_that_escape(self, tool, workspace) -> None:
        """A symlink inside an allowed folder can still point out of it."""
        (workspace / "escape").symlink_to(workspace.parent / "private")
        result = await tool.execute({"operation": "list", "path": str(workspace)})
        assert "escape" not in [e["name"] for e in result.data["entries"]]


class TestDuplicates:
    async def test_groups_by_content_not_name(self, tool, workspace) -> None:
        (workspace / "a.txt").write_text("same content")
        (workspace / "b.txt").write_text("same content")
        (workspace / "c.txt").write_text("different")
        result = await tool.execute({"operation": "find_duplicates", "path": str(workspace)})
        groups = result.data["groups"]
        assert len(groups) == 1
        assert len(groups[0]["duplicates"]) == 1
        assert result.data["wastedBytes"] == len("same content")

    async def test_empty_files_are_ignored(self, tool, workspace) -> None:
        (workspace / "a.txt").write_text("")
        (workspace / "b.txt").write_text("")
        result = await tool.execute({"operation": "find_duplicates", "path": str(workspace)})
        assert result.data["groups"] == []


class TestScopesAndRisk:
    @pytest.mark.parametrize(
        ("operation", "scope"),
        [
            ("list", Scope.FS_READ),
            ("read", Scope.FS_READ),
            ("create_folder", Scope.FS_WRITE),
            ("write", Scope.FS_WRITE),
            ("delete", Scope.FS_DELETE),
            ("delete_permanently", Scope.FS_DELETE),
        ],
    )
    def test_each_operation_declares_its_scope(self, tool, operation: str, scope: Scope) -> None:
        assert scope in tool.scopes_for({"operation": operation})

    def test_move_needs_read_as_well_as_write(self, tool) -> None:
        """Moving removes from the source as well as writing to the destination."""
        scopes = tool.scopes_for({"operation": "move"})
        assert Scope.FS_WRITE in scopes
        assert Scope.FS_READ in scopes

    async def test_unknown_operation_is_rejected(self, tool) -> None:
        from jarvis.tools.base import ToolInputInvalid

        with pytest.raises(ToolInputInvalid, match="Unknown operation"):
            await tool.preview({"operation": "destroy_everything"})


class TestObservation:
    """The executor verifies claims against these snapshots."""

    async def test_reports_existence_before_and_after(self, tool, workspace) -> None:
        target = workspace / "x.txt"
        assert (await tool.observe({"operation": "write", "path": str(target)}))["exists"] is False
        target.write_text("hi")
        after = await tool.observe({"operation": "write", "path": str(target)})
        assert after["exists"] is True
        assert after["size"] == 2

    async def test_unobservable_paths_return_empty(self, tool) -> None:
        assert await tool.observe({"operation": "list"}) == {}
        assert await tool.observe({"operation": "read", "path": "/etc/passwd"}) == {}

"""Path jail. The most security-critical unit in the project.

Every test here is an escape attempt. If one of these starts passing when it
should fail, the agent can reach outside the folders the user allowed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jarvis.config import folders
from jarvis.governance.pathjail import Access, PathDenied, PathJail


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME_OVERRIDE", str(tmp_path))
    folders.reset_cache()
    for name in ("Desktop", "Documents", "Downloads", "Pictures"):
        (tmp_path / name).mkdir()
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "private.txt").write_text("classified")
    yield tmp_path
    folders.reset_cache()


@pytest.fixture
def jail(home):
    return PathJail([home / "Desktop", home / "Documents"])


class TestAllowed:
    def test_file_inside_an_allowed_root(self, jail, home) -> None:
        resolved = jail.check(home / "Desktop" / "notes.txt")
        assert resolved.path == home / "Desktop" / "notes.txt"
        assert resolved.root == home / "Desktop"

    def test_nested_subdirectory(self, jail, home) -> None:
        assert jail.is_allowed(home / "Desktop" / "a" / "b" / "c.txt")

    def test_the_root_itself(self, jail, home) -> None:
        assert jail.is_allowed(home / "Desktop")

    def test_known_folder_token(self, jail, home) -> None:
        assert jail.check("Desktop").path == home / "Desktop"

    def test_token_with_a_subpath(self, jail, home) -> None:
        expected = home / "Desktop" / "work" / "report.pdf"
        assert jail.check("Desktop/work/report.pdf").path == expected

    def test_a_path_that_does_not_exist_yet(self, jail, home) -> None:
        """Writing a new file must be judged by where its parent really points."""
        resolved = jail.check(home / "Desktop" / "new.txt", Access.WRITE)
        assert resolved.existed is False
        assert resolved.path == home / "Desktop" / "new.txt"


class TestTraversal:
    @pytest.mark.parametrize(
        "attempt",
        [
            "Desktop/../../secrets/private.txt",
            "Desktop/../secrets/private.txt",
            "Desktop/./../../secrets",
            "Desktop/a/b/../../../secrets",
        ],
    )
    def test_dot_dot_cannot_escape(self, jail, attempt: str) -> None:
        with pytest.raises(PathDenied) as exc:
            jail.check(attempt)
        assert exc.value.reason in {"outside_allowed", "forbidden_root"}

    @pytest.mark.parametrize("attempt", ["Desktop/..../x", "Desktop/....//....//secrets", "..../y"])
    def test_dot_runs_are_refused(self, jail, attempt: str) -> None:
        """Three or more dots is never a real folder, and `....//` collapsing to
        `../` is a long-standing Windows traversal trick."""
        with pytest.raises(PathDenied) as exc:
            jail.check(attempt)
        assert exc.value.reason == "dot_run"

    @pytest.mark.parametrize(
        "attempt", ["Desktop/System32./x", "Desktop/notes.txt ", "Desktop/a /b"]
    )
    def test_trailing_dots_and_spaces_are_refused(self, jail, attempt: str) -> None:
        """Windows strips them, so the string checked and the file opened differ."""
        with pytest.raises(PathDenied) as exc:
            jail.check(attempt)
        assert exc.value.reason == "trailing_junk"

    def test_sibling_folder_is_not_covered(self, jail, home) -> None:
        """Granting Desktop must not grant Downloads."""
        with pytest.raises(PathDenied, match="outside the folders"):
            jail.check(home / "Downloads" / "x.txt")

    def test_prefix_collision_is_not_containment(self, home) -> None:
        """`Desktop2` must not be treated as inside `Desktop`."""
        (home / "Desktop2").mkdir()
        jail = PathJail([home / "Desktop"])
        with pytest.raises(PathDenied):
            jail.check(home / "Desktop2" / "x.txt")

    def test_relative_paths_are_refused(self, jail) -> None:
        with pytest.raises(PathDenied) as exc:
            jail.check("notes.txt")
        assert exc.value.reason == "relative"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
class TestSymlinks:
    """The subtle case: the string looks fine, the filesystem disagrees."""

    def test_symlink_pointing_outside_is_refused(self, jail, home) -> None:
        (home / "Desktop" / "escape").symlink_to(home / "secrets")
        with pytest.raises(PathDenied) as exc:
            jail.check(home / "Desktop" / "escape" / "private.txt")
        assert exc.value.reason == "outside_allowed"

    def test_symlinked_parent_of_a_new_file_is_refused(self, jail, home) -> None:
        """Creating a file through a symlinked directory must not escape."""
        (home / "Desktop" / "out").symlink_to(home / "secrets")
        with pytest.raises(PathDenied):
            jail.check(home / "Desktop" / "out" / "new.txt", Access.WRITE)

    def test_symlink_staying_inside_is_fine(self, jail, home) -> None:
        (home / "Desktop" / "sub").mkdir()
        (home / "Desktop" / "link").symlink_to(home / "Desktop" / "sub")
        assert jail.is_allowed(home / "Desktop" / "link" / "f.txt")

    def test_resolution_loop_is_handled(self, jail, home) -> None:
        a, b = home / "Desktop" / "a", home / "Desktop" / "b"
        a.symlink_to(b)
        b.symlink_to(a)
        with pytest.raises(PathDenied):
            jail.check(a / "x")


class TestForbiddenRoots:
    """Locations no grant can open."""

    def test_system_paths_are_refused(self, home) -> None:
        wide_open = PathJail([Path("/")] if sys.platform != "win32" else [Path("C:\\")])
        target = "/etc/passwd" if sys.platform != "win32" else r"C:\Windows\System32\config\SAM"
        with pytest.raises(PathDenied) as exc:
            wide_open.check(target)
        assert exc.value.reason == "forbidden_root"

    def test_jarvis_own_data_is_refused(self, home, monkeypatch, tmp_path) -> None:
        """Jarvis must not be able to rewrite its own audit log or database."""
        data = tmp_path / "jarvisdata"
        data.mkdir()
        monkeypatch.setenv("JARVIS_DATA_DIR", str(data))
        from jarvis.config import paths

        paths.reset_cache()
        jail = PathJail([tmp_path])
        with pytest.raises(PathDenied) as exc:
            jail.check(data / "jarvis.db", Access.WRITE)
        assert exc.value.reason == "forbidden_root"
        paths.reset_cache()


class TestWindowsSpecific:
    """Implemented and unit-tested here; behaviour against a real Windows
    filesystem still needs verifying on Windows."""

    @pytest.mark.parametrize("name", ["CON", "nul", "COM1", "LPT9", "con.txt", "AUX.log"])
    def test_device_names_are_refused(self, jail, name: str) -> None:
        with pytest.raises(PathDenied) as exc:
            jail.check(f"Desktop/{name}")
        assert exc.value.reason == "device_name"

    @pytest.mark.parametrize(
        "raw", [r"\\.\PhysicalDrive0", r"\\?\GLOBALROOT\Device\Harddisk0", "//./COM1"]
    )
    def test_device_paths_are_refused(self, jail, raw: str) -> None:
        with pytest.raises(PathDenied) as exc:
            jail.check(raw)
        assert exc.value.reason == "device_path"

    def test_unc_paths_are_refused(self, jail) -> None:
        """This assistant acts on this computer only."""
        with pytest.raises(PathDenied) as exc:
            jail.check(r"\\fileserver\share\data.xlsx")
        assert exc.value.reason == "unc_path"

    def test_alternate_data_streams_are_refused(self, jail) -> None:
        with pytest.raises(PathDenied) as exc:
            jail.check(r"Desktop\report.txt:hidden")
        assert exc.value.reason == "ads"

    def test_trailing_dot_cannot_dodge_a_deny(self) -> None:
        """`System32.` opens `System32` on Windows but is a different string."""
        from jarvis.governance.pathjail import _is_within

        assert _is_within(Path(r"C:\Windows\System32."), Path(r"C:\Windows"))
        assert _is_within(Path(r"c:\windows\system32"), Path(r"C:\Windows"))


class TestSecretFiles:
    """Credential files stay unreadable even inside an allowed folder."""

    @pytest.mark.parametrize(
        "name", [".env", "id_rsa", "id_ed25519", "server.pem", "cert.pfx", ".git-credentials"]
    )
    def test_credential_filenames_are_refused(self, jail, name: str) -> None:
        with pytest.raises(PathDenied) as exc:
            jail.check(f"Desktop/{name}")
        assert exc.value.reason == "secret_file"

    def test_credential_directories_are_refused(self, jail) -> None:
        with pytest.raises(PathDenied) as exc:
            jail.check("Desktop/.ssh/known_hosts")
        assert exc.value.reason == "secret_file"

    def test_ordinary_documents_are_fine(self, jail) -> None:
        for name in ["report.pdf", "notes.txt", "budget.xlsx", "photo.jpg", "key-points.docx"]:
            assert jail.is_allowed(f"Desktop/{name}"), name


class TestMalformedInput:
    @pytest.mark.parametrize(
        ("raw", "reason"), [("", "empty"), ("   ", "empty"), ("Desktop/a\x00b", "null_byte")]
    )
    def test_rejected(self, jail, raw: str, reason: str) -> None:
        with pytest.raises(PathDenied) as exc:
            jail.check(raw)
        assert exc.value.reason == reason

    def test_environment_variables_do_not_expand(self, jail) -> None:
        """Model output naming an env var must not reach that folder."""
        for attempt in ["%WINDIR%\\System32", "$HOME/secrets", "${HOME}/secrets"]:
            with pytest.raises(PathDenied):
                jail.check(attempt)


class TestReporting:
    def test_denial_names_the_allowed_folders(self, jail, home) -> None:
        """An error the user cannot act on is a bad error."""
        with pytest.raises(PathDenied) as exc:
            jail.check(home / "Downloads" / "x")
        assert "Desktop" in exc.value.message
        assert "Privacy settings" in exc.value.message

    def test_describe_lists_both_sets(self, jail) -> None:
        described = jail.describe()
        assert described["allowed"]
        assert described["forbidden"]

    def test_check_many_stops_at_the_first_bad_path(self, jail, home) -> None:
        with pytest.raises(PathDenied):
            jail.check_many([home / "Desktop" / "ok.txt", "/etc/passwd"])

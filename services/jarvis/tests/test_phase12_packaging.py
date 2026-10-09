"""Packaging: the build, and the model fetcher.

The Phase 12 gate — "AI-Assistant-Setup.exe installs and runs on a clean
Windows 10 and 11 VM" — **cannot be met from this container**. PyInstaller does
not cross-compile and neither does the WebView2 shell, so the Windows installer
has never been built, let alone installed. docs/PHASES.md says so plainly.

What *can* be verified here is verified here: the PyInstaller spec produces a
working binary on this platform, the build script's logic, the Tauri
configuration's shape, and all of the model fetcher — including the parts that
are easy to get wrong and invisible when you do, like a failed download leaving
a truncated file behind.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from jarvis.config import models, paths

REPO = Path(__file__).resolve().parents[3]


# ── the model fetcher ────────────────────────────────────────────────────
class TestModelCatalogue:
    def test_every_model_says_what_it_enables_and_what_it_costs(self) -> None:
        """A download with no stated size is a surprise on a metered connection."""
        for spec in models.CATALOGUE:
            assert spec.enables, f"{spec.key} does not say what it is for"
            assert spec.size_mb > 0, f"{spec.key} does not say how big it is"
            assert spec.name

    def test_nothing_is_fetchable_without_a_checksum(self) -> None:
        """A model is loaded and executed, so an unverifiable download is refused."""
        for spec in models.CATALOGUE:
            if not spec.sha256:
                assert not spec.fetchable

    def test_a_model_the_list_cannot_fetch_explains_itself(self) -> None:
        """Two reasons, not one, and the difference decides what to do next.

        This asserted "no verified checksum" for every unfetchable model,
        which was the whole story only while unfetchable also meant
        unobtainable. The speech model is fetched by faster-whisper itself,
        so that message sent people off to install a file by hand that Jarvis
        downloads perfectly well from the Voice panel — and the model list,
        saying "Unavailable", was where voice looked like a dead end.
        """
        unfetchable = [m for m in models.CATALOGUE if not m.fetchable]
        assert unfetchable, "this test is about the current state of the catalogue"
        for spec in unfetchable:
            reason = str(spec.to_dict()["reason"])
            assert reason, f"{spec.key} must say why the list cannot fetch it"
            if spec.prepare_route:
                # Say where it comes from, not "install it by hand".
                assert "Voice panel" in reason, spec.key
                assert "by hand" not in reason, spec.key
            else:
                assert "no verified checksum" in reason, spec.key

    def test_an_obtainable_model_is_not_reported_as_a_dead_end(self) -> None:
        """`fetchable` and `obtainable` are different questions.

        Conflating them is what made the speech model unreachable: the list
        showed it as Unavailable, and the one route that downloads it was
        never called by anything.
        """
        speech = models.BY_KEY["whisper-base-en"]
        assert not speech.fetchable, "it has no checksum, so the list must not fetch it"
        assert speech.obtainable, "but Jarvis can still get it"
        assert speech.prepare_route == "/voice/stt/prepare"
        assert speech.to_dict()["prepareRoute"] == "/voice/stt/prepare"

    def test_the_summary_adds_up_what_is_missing(self) -> None:
        summary = models.summary()
        assert summary["totalCount"] == len(models.CATALOGUE)
        assert summary["bundled"] is False
        assert summary["missingMb"] > 0

    def test_voice_and_memory_models_are_both_listed(self) -> None:
        keys = set(models.BY_KEY)
        assert {"whisper-base-en", "piper-en-us", "openwakeword-hey-jarvis"} <= keys
        assert "minilm-l6-v2" in keys, "semantic memory's model must be listed too"


class TestModelFetch:
    def test_fetching_something_unverifiable_is_refused(self) -> None:
        with pytest.raises(models.ModelError, match="no verified checksum"):
            models.fetch("whisper-base-en", opener=lambda url: io.BytesIO(b"x"))

    def test_fetching_an_unknown_model_says_so(self) -> None:
        with pytest.raises(models.ModelError, match="no model called"):
            models.fetch("not-a-model")

    def _verifiable(self, monkeypatch, payload: bytes, folder: str = "t") -> models.ModelSpec:
        spec = models.ModelSpec(
            key="testable",
            name="Testable",
            enables="tests",
            filename="model.bin",
            url="https://example.invalid/model.bin",
            size_mb=1,
            sha256=__import__("hashlib").sha256(payload).hexdigest(),
            folder=folder,
        )
        monkeypatch.setitem(models.BY_KEY, "testable", spec)
        return spec

    def test_a_good_download_is_verified_and_installed(self, monkeypatch) -> None:
        payload = b"a model, allegedly" * 100
        spec = self._verifiable(monkeypatch, payload)
        path = models.fetch("testable", opener=lambda url: io.BytesIO(payload))
        assert path.read_bytes() == payload
        assert spec.installed()
        assert models.verify_installed(spec)[0] is True

    def test_a_corrupted_download_is_deleted_not_kept(self, monkeypatch) -> None:
        """A model that is not the one expected is a code-execution problem."""
        spec = self._verifiable(monkeypatch, b"the real thing", folder="corrupt")
        with pytest.raises(models.ChecksumMismatch):
            models.fetch("testable", opener=lambda url: io.BytesIO(b"something else"))
        assert not spec.installed()
        assert not spec.path().with_suffix(".bin.part").exists()

    def test_an_interrupted_download_leaves_nothing_behind(self, monkeypatch) -> None:
        """A truncated model would fail mysteriously at load time instead."""
        spec = self._verifiable(monkeypatch, b"whole", folder="interrupted")

        class Broken(io.RawIOBase):
            def readinto(self, _b: object) -> int:
                raise OSError("the connection dropped")

            def __enter__(self) -> Broken:
                return self

            def __exit__(self, *_: object) -> None:
                return None

        with pytest.raises(models.ModelError, match="Could not download"):
            models.fetch("testable", opener=lambda url: Broken())
        assert not spec.installed()
        assert list(spec.path().parent.glob("*.part")) == []

    def test_a_non_https_url_is_refused(self, monkeypatch) -> None:
        spec = models.ModelSpec(
            key="sneaky",
            name="Sneaky",
            enables="nothing good",
            filename="x.bin",
            url="file:///etc/passwd",
            size_mb=1,
            sha256="00" * 32,
            folder="sneaky",
        )
        monkeypatch.setitem(models.BY_KEY, "sneaky", spec)
        with pytest.raises(models.ModelError, match="not https"):
            models.fetch("sneaky", opener=lambda url: io.BytesIO(b"x"))

    def test_an_already_installed_model_is_not_refetched(self, monkeypatch) -> None:
        payload = b"already here"
        spec = self._verifiable(monkeypatch, payload, folder="cached")
        models.fetch("testable", opener=lambda url: io.BytesIO(payload))

        def must_not_be_called(url: str) -> io.BytesIO:
            raise AssertionError("it downloaded a model that was already present")

        assert models.fetch("testable", opener=must_not_be_called) == spec.path()

    def test_removing_a_model_deletes_the_file(self, monkeypatch) -> None:
        payload = b"temporary"
        spec = self._verifiable(monkeypatch, payload, folder="removable")
        models.fetch("testable", opener=lambda url: io.BytesIO(payload))
        assert models.remove("testable") is True
        assert not spec.installed()
        assert models.remove("testable") is False

    def test_a_model_on_disk_that_does_not_match_is_reported(self, monkeypatch) -> None:
        spec = self._verifiable(monkeypatch, b"expected", folder="tampered")
        spec.path().parent.mkdir(parents=True, exist_ok=True)
        spec.path().write_bytes(b"not what was expected")
        ok, detail = models.verify_installed(spec)
        assert ok is False
        assert "does not match" in detail


class TestModelApi:
    async def test_listing_says_models_are_not_bundled(self, client) -> None:
        body = (await client.get("/models")).json()
        assert body["bundled"] is False
        assert body["totalCount"] == len(models.CATALOGUE)
        assert all("sizeMb" in m for m in body["models"])

    async def test_fetching_an_unknown_model_is_404(self, client) -> None:
        assert (await client.post("/models/nope/fetch")).status_code == 404

    async def test_fetching_an_unverifiable_model_explains_rather_than_tries(self, client) -> None:
        response = await client.post("/models/whisper-base-en/fetch")
        assert response.status_code == 502
        assert "no verified checksum" in response.json()["message"]

    async def test_verifying_something_absent_says_it_is_absent(self, client) -> None:
        body = (await client.get("/models/whisper-base-en/verify")).json()
        assert body["ok"] is False
        assert "not installed" in body["detail"]

    async def test_model_endpoints_need_a_token(self, app) -> None:
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
            assert (await anon.get("/models")).status_code == 401

    async def test_models_live_under_the_data_directory(self, client) -> None:
        body = (await client.get("/models")).json()
        assert str(paths.data_dir()) in body["directory"]


# ── the build pipeline ───────────────────────────────────────────────────
class TestBuildConfiguration:
    def test_the_pyinstaller_spec_exists_and_builds_one_file(self) -> None:
        spec = REPO / "services" / "jarvis" / "jarvis-core.spec"
        assert spec.is_file()
        text = spec.read_text()
        assert 'name="jarvis-core"' in text
        assert "console=True" in text, (
            "a windowed build has no stdout, and the handshake is a line on stdout"
        )
        assert "upx=False" in text, "UPX makes antivirus heuristics much less happy"

    def test_the_spec_excludes_the_heavy_optional_extras(self) -> None:
        """Bundling them would quintuple an installer for features many never use."""
        text = (REPO / "services" / "jarvis" / "jarvis-core.spec").read_text()
        for extra in ("onnxruntime", "playwright", "torch", "faster_whisper"):
            assert extra in text

    def test_the_tauri_config_ships_the_core_as_a_resource_folder(self) -> None:
        """A tree, so `resources` rather than `externalBin`.

        `externalBin` carries one renamed executable. The core is a one-folder
        PyInstaller bundle whose executable finds `_internal` beside itself, so
        shipping it that way would deliver the executable alone — an installer
        that builds and then cannot start its own core.
        """
        conf = json.loads((REPO / "apps" / "desktop" / "src-tauri" / "tauri.conf.json").read_text())
        assert "externalBin" not in conf["bundle"], (
            "a one-folder bundle cannot ship as an externalBin"
        )
        assert conf["bundle"]["resources"]["binaries/jarvis-core"] == "core"
        assert conf["bundle"]["targets"] == ["nsis"]

    def test_the_spec_builds_one_folder_rather_than_one_file(self) -> None:
        """What kept startup at eight seconds.

        A one-file bundle unpacks 600 MB to a temporary directory on every
        launch before Python starts. Measured launch-to-handshake: 7.8-8.5 s
        one-file against 1.2-1.4 s one-folder, with an unfrozen floor of
        0.8-1.0 s — so almost all of it was extraction, not the app's own boot.

        `EXE(exclude_binaries=True)` and `COLLECT` have to agree: without the
        flag PyInstaller silently builds one-file again and the binaries land
        in both places.
        """
        text = (REPO / "services" / "jarvis" / "jarvis-core.spec").read_text()
        assert "COLLECT(" in text, "one-folder needs a COLLECT step"
        assert "exclude_binaries=True" in text, (
            "without this the EXE is still a self-extracting one-file bundle"
        )

    def test_the_installer_runs_without_administrator_rights(self) -> None:
        """A per-user install is one fewer UAC prompt and one fewer reason to refuse."""
        conf = json.loads((REPO / "apps" / "desktop" / "src-tauri" / "tauri.conf.json").read_text())
        assert conf["bundle"]["windows"]["nsis"]["installMode"] == "currentUser"

    def test_the_uninstaller_asks_before_deleting_user_data(self) -> None:
        hooks = REPO / "apps" / "desktop" / "src-tauri" / "nsis" / "hooks.nsh"
        assert hooks.is_file()
        text = hooks.read_text()
        assert "MB_DEFBUTTON2" in text, "the default must be to keep the data"
        assert "NSIS_HOOK_POSTUNINSTALL" in text
        assert "conversation history" in text

    def test_the_uninstaller_stops_the_core_before_replacing_it(self) -> None:
        """A running executable cannot be overwritten, so an upgrade would fail."""
        text = (REPO / "apps" / "desktop" / "src-tauri" / "nsis" / "hooks.nsh").read_text()
        assert "NSIS_HOOK_PREINSTALL" in text
        assert "taskkill" in text

    def test_the_build_script_smoke_tests_what_it_built(self) -> None:
        """PyInstaller succeeds happily with a missing hidden import."""
        text = (REPO / "scripts" / "build_core.py").read_text()
        assert "def smoke_test" in text
        assert "handshake" in text

    def test_the_build_script_puts_the_core_where_tauri_looks_for_it(self) -> None:
        """The two have to name the same path, or the installer ships no core.

        This used to assert the script computed a target triple, which is what
        `externalBin` needed. The folder bundle does not get renamed, so what
        matters now is agreement: `tauri.conf.json` declares a resource path
        and `build_core.py` writes to one, and nothing else connects them. Get
        it wrong and the build succeeds with an installer whose core is absent.
        """
        conf = json.loads((REPO / "apps" / "desktop" / "src-tauri" / "tauri.conf.json").read_text())
        declared = next(
            path for path in conf["bundle"]["resources"] if path.endswith("jarvis-core")
        )
        # Relative to src-tauri in the config; the script builds it from the
        # repository root, so compare the resolved locations.
        expected = (REPO / "apps" / "desktop" / "src-tauri" / declared).resolve()

        script = (REPO / "scripts" / "build_core.py").read_text()
        assert 'SIDECAR_DIR = ROOT / "apps" / "desktop" / "src-tauri" / "binaries"' in script
        assert "destination = SIDECAR_DIR / NAME" in script
        assert 'NAME = "jarvis-core"' in script
        assert expected == (REPO / "apps/desktop/src-tauri/binaries/jarvis-core").resolve(), (
            f"tauri.conf.json declares {declared}, which is not where build_core.py writes"
        )

    def test_the_build_script_copies_the_whole_tree(self) -> None:
        """One file is not enough any more, and a merge is not safe.

        The executable needs `_internal` beside it, so copying the binary
        alone produces a core that cannot start. And the destination is
        replaced rather than merged: a stale .pyd from an earlier build with
        different dependencies gets loaded in preference to nothing and fails
        a long way from the cause.
        """
        text = (REPO / "scripts" / "build_core.py").read_text()
        assert "copytree" in text
        assert "rmtree" in text

    def test_the_windows_script_refuses_to_build_on_failing_tests(self) -> None:
        text = (REPO / "scripts" / "build_windows.ps1").read_text()
        assert "not building an installer" in text

    def test_the_windows_script_warns_about_an_unsigned_build(self) -> None:
        """SmartScreen warns every user; an unsigned release is not acceptable."""
        text = (REPO / "scripts" / "build_windows.ps1").read_text()
        assert "TAURI_SIGNING_PRIVATE_KEY" in text
        assert "SmartScreen" in text

    def test_the_windows_script_says_the_gate_is_not_met_by_building(self) -> None:
        text = (REPO / "scripts" / "build_windows.ps1").read_text()
        assert "has NOT been tested on a clean Windows install" in text


class TestVersionsAgree:
    """Five files declare the version, and all five must say the same thing.

    This used to assert only that two of them were non-empty, under a name
    saying they reported the same version. The release workflow checks
    `tauri.conf.json` against the tag and nothing checks the rest, so a bump
    that missed a file drifted silently: the installer's version, the shell's
    crate version and the version the core reports in its handshake and bug
    reports could all disagree.
    """

    def _declarations(self) -> dict[str, str]:
        desktop = REPO / "apps" / "desktop"
        found: dict[str, str] = {}

        found["tauri.conf.json"] = json.loads(
            (desktop / "src-tauri" / "tauri.conf.json").read_text()
        )["version"]
        found["package.json"] = json.loads((desktop / "package.json").read_text())["version"]

        # The first `version =` under `[package]`, not a dependency's.
        cargo = (desktop / "src-tauri" / "Cargo.toml").read_text()
        match = re.search(r'^\[package\][^\[]*?^version = "([^"]+)"', cargo, re.M | re.S)
        assert match is not None, "no [package] version in Cargo.toml"
        found["Cargo.toml"] = match.group(1)

        pyproject = (REPO / "services" / "jarvis" / "pyproject.toml").read_text()
        match = re.search(r'^\[project\][^\[]*?^version = "([^"]+)"', pyproject, re.M | re.S)
        assert match is not None, "no [project] version in pyproject.toml"
        found["pyproject.toml"] = match.group(1)

        app_py = (REPO / "services" / "jarvis" / "jarvis" / "app.py").read_text()
        match = re.search(r'^VERSION = "([^"]+)"', app_py, re.M)
        assert match is not None, "no VERSION in app.py"
        found["app.py"] = match.group(1)

        return found

    def test_every_file_declares_the_same_version(self) -> None:
        found = self._declarations()
        assert len(set(found.values())) == 1, "these disagree about the version: " + ", ".join(
            f"{where}={what}" for where, what in sorted(found.items())
        )

    def test_the_version_looks_like_a_version(self) -> None:
        """The tag check compares against `v` + this, so `0.2.1`, not `v0.2.1`."""
        for where, what in self._declarations().items():
            assert re.fullmatch(r"\d+\.\d+\.\d+", what), f"{where} declares {what!r}"

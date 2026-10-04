"""`--selfcheck`: which optional features a build actually has.

`scripts/build_core.py` fails the Windows build when a package installed for it
did not make it into the PyInstaller bundle, and this is how it asks. That makes
the output a contract rather than a convenience: if it stops being a single JSON
line on stdout, or stops exiting 0, the build check silently stops checking.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from jarvis.diagnostics import bundle


class TestTheReport:
    def test_every_package_is_accounted_for(self) -> None:
        report = bundle.report()
        assert set(report["packages"]) == set(bundle.OPTIONAL_PACKAGES)
        for name, row in report["packages"].items():
            assert isinstance(row["available"], bool), name
            assert row["purpose"], f"{name} has no stated purpose"

    def test_present_and_missing_partition_the_packages(self) -> None:
        report = bundle.report()
        assert set(report["present"]) | set(report["missing"]) == set(bundle.OPTIONAL_PACKAGES)
        assert not set(report["present"]) & set(report["missing"])

    def test_a_purpose_reads_as_a_capability_not_a_package_name(self) -> None:
        """The audience is someone reading a build log or a bug report."""
        for name, purpose in bundle.OPTIONAL_PACKAGES.items():
            assert " " in purpose, f"{name}: {purpose!r} is not a description"
            assert not purpose.endswith("."), f"{name}: {purpose!r} should not end in a period"


class TestAvailability:
    def test_something_installed_is_reported_present(self) -> None:
        # pydantic is a hard dependency, so this cannot be skipped away.
        ok, detail = bundle.available("pydantic")
        assert ok
        assert detail == ""

    def test_something_absent_is_reported_with_the_reason(self) -> None:
        ok, detail = bundle.available("a_package_that_does_not_exist_xyz")
        assert not ok
        assert "ModuleNotFoundError" in detail

    def test_a_failure_that_is_not_an_importerror_is_still_caught(self, monkeypatch) -> None:
        """The failure a frozen bundle actually produces.

        A native extension whose shared library did not make it in raises
        OSError, not ImportError — `import sounddevice` without PortAudio is
        exactly this. Catching only ImportError would let it crash the check
        that exists to notice it.
        """

        def explode(name: str) -> None:
            raise OSError("libportaudio.so.2: cannot open shared object file")

        monkeypatch.setattr(bundle.importlib, "import_module", explode)
        ok, detail = bundle.available("sounddevice")
        assert not ok
        assert "OSError" in detail
        assert "libportaudio" in detail


class TestTheCommand:
    @pytest.fixture
    def run(self, tmp_path):
        def go() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-m", "jarvis", "--selfcheck"],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
                env={
                    "PATH": "/usr/bin:/bin",
                    "JARVIS_DATA_DIR": str(tmp_path / "data"),
                    "JARVIS_CONFIG_DIR": str(tmp_path / "config"),
                },
            )

        return go

    def test_prints_one_json_line_and_exits_zero(self, run) -> None:
        result = run()
        assert result.returncode == 0, result.stderr[-600:]
        lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
        assert len(lines) == 1, f"expected a single line, got {lines}"

        report = json.loads(lines[0])
        assert report["version"]
        assert set(report["packages"]) == set(bundle.OPTIONAL_PACKAGES)

    def test_does_not_leave_a_data_directory_behind(self, run, tmp_path) -> None:
        """It answers "what is in this binary", and runs on a build machine.

        Creating the data tree and writing a default config as a side effect of
        asking that question would leave state on a CI runner, and would mean
        the check could not be run against a binary without one.
        """
        result = run()
        assert result.returncode == 0
        assert not (tmp_path / "data").exists()
        assert not (tmp_path / "config").exists()

    def test_does_not_bind_a_port_or_start_serving(self, run) -> None:
        result = run()
        assert result.returncode == 0
        # The handshake is what a started core prints; this must not be one.
        assert '"jarvis": "ready"' not in result.stdout
        assert '"token"' not in result.stdout

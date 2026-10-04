"""The protected-process list.

Checked below the policy engine, so no mode, scope or confirmation reaches it.
These tests are the only guarantee that holds on a machine this repository's CI
cannot run on, so both the Windows and POSIX lists are exercised here.
"""

from __future__ import annotations

import os

import pytest

from jarvis.platform_.protected import check, is_protected


class TestSystemCritical:
    @pytest.mark.parametrize(
        "name",
        [
            "csrss.exe",
            "wininit.exe",
            "winlogon.exe",
            "services.exe",
            "lsass.exe",
            "smss.exe",
            "svchost.exe",
            "explorer.exe",
            "dwm.exe",
        ],
    )
    def test_core_windows_processes_are_refused(self, name: str) -> None:
        """Checked on every platform: gating this list behind a Windows check
        would make it untestable anywhere but Windows."""
        protected, reason = check(5000, name)
        assert protected
        assert "crash" in reason or "log you out" in reason

    @pytest.mark.parametrize("pid", [0, 4])
    def test_kernel_pids_are_refused(self, pid: int) -> None:
        assert is_protected(pid, "System")

    def test_posix_init_is_refused(self) -> None:
        assert is_protected(1, "systemd") or os.name == "nt"


class TestSecuritySoftware:
    @pytest.mark.parametrize(
        "name",
        [
            "MsMpEng.exe",
            "SecurityHealthService.exe",
            "MsSense.exe",
            "avp.exe",
            "mbamservice.exe",
            "SentinelAgent.exe",
            "CSFalconService.exe",
        ],
    )
    def test_antivirus_is_refused(self, name: str) -> None:
        """Disabling protection is what malware does. Never routine."""
        protected, reason = check(5000, name)
        assert protected
        assert "unprotected" in reason

    def test_the_reason_rules_out_a_confirmation(self) -> None:
        _, reason = check(5000, "MsMpEng.exe")
        assert "will not" in reason


class TestSelfProtection:
    def test_jarvis_cannot_end_itself(self) -> None:
        protected, reason = check(os.getpid(), "python")
        assert protected
        assert "Jarvis itself" in reason

    @pytest.mark.parametrize("name", ["jarvis.exe", "jarvis-core.exe"])
    def test_jarvis_binaries_are_refused(self, name: str) -> None:
        assert is_protected(9999, name)


class TestSystemDirectories:
    @pytest.mark.parametrize(
        "exe",
        [
            r"C:\Windows\System32\unknown.exe",
            r"C:\Windows\SysWOW64\thing.exe",
            "C:/Windows/System32/other.exe",
        ],
    )
    def test_anything_in_system32_is_refused(self, exe: str) -> None:
        """A process is system software even when its name is not on the list."""
        protected, reason = check(5000, "unknown.exe", exe)
        assert protected
        assert "system directory" in reason

    def test_case_and_separators_do_not_matter(self) -> None:
        assert is_protected(5000, "x.exe", r"c:\WINDOWS\system32\x.exe")


class TestOrdinaryPrograms:
    @pytest.mark.parametrize(
        ("name", "exe"),
        [
            ("chrome.exe", r"C:\Program Files\Google\Chrome\chrome.exe"),
            ("notepad++.exe", r"C:\Program Files\Notepad++\notepad++.exe"),
            ("Code.exe", r"C:\Users\me\AppData\Local\Programs\VSCode\Code.exe"),
            ("spotify.exe", ""),
        ],
    )
    def test_user_programs_can_be_ended(self, name: str, exe: str) -> None:
        assert not is_protected(5000, name, exe)

    def test_a_similar_name_is_not_over_matched(self) -> None:
        """`explorer.exe` is protected; `explorerpp.exe` is a different program."""
        assert is_protected(5000, "explorer.exe")
        assert not is_protected(5000, "explorerpp.exe")

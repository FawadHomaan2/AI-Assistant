"""Matching a spoken name to installed software.

Pure logic, so it is fully verified here even though launching is not.
"""

from __future__ import annotations

import pytest

from jarvis.platform_.app_catalog import normalise, pick, resolve
from jarvis.platform_.types import AppInfo


@pytest.fixture
def catalogue() -> list[AppInfo]:
    names = [
        ("chrome", "Google Chrome"),
        ("msedge", "Microsoft Edge"),
        ("firefox", "Mozilla Firefox"),
        ("code", "Visual Studio Code"),
        ("winword", "Microsoft Word"),
        ("wordpad", "WordPad"),
        ("excel", "Microsoft Excel"),
        ("notepad", "Notepad"),
        ("spotify", "Spotify"),
        ("teams", "Microsoft Teams"),
        ("calc", "Calculator"),
    ]
    return [AppInfo(key=k, name=n, launch_target=f"C:\\{k}.exe") for k, n in names]


class TestNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Google Chrome", "google chrome"),
            ("  the Chrome app ", "chrome"),
            ("Visual Studio Code!", "visual studio code"),
            ("my Spotify application", "spotify"),
        ],
    )
    def test_strips_noise_and_punctuation(self, raw: str, expected: str) -> None:
        assert normalise(raw) == expected


class TestResolution:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("chrome", "Google Chrome"),
            ("google chrome", "Google Chrome"),
            ("edge", "Microsoft Edge"),
            ("firefox", "Mozilla Firefox"),
            ("vs code", "Visual Studio Code"),
            ("vscode", "Visual Studio Code"),
            ("visual studio code", "Visual Studio Code"),
            ("word", "Microsoft Word"),
            ("wordpad", "WordPad"),
            ("excel", "Microsoft Excel"),
            ("spotify", "Spotify"),
            ("teams", "Microsoft Teams"),
            ("calculator", "Calculator"),
        ],
    )
    def test_common_names(self, catalogue, query: str, expected: str) -> None:
        app, _ = pick(query, catalogue)
        assert app is not None, f"{query!r} matched nothing"
        assert app.name == expected

    def test_an_uninstalled_program_matches_nothing(self, catalogue) -> None:
        app, matches = pick("photoshop", catalogue)
        assert app is None
        assert matches == []

    # Guessing between candidates opens the wrong program.
    def test_an_ambiguous_name_asks_rather_than_guessing(self, catalogue) -> None:
        app, matches = pick("microsoft", catalogue)
        assert app is None
        assert len(matches) >= 3

    def test_vendor_words_do_not_create_matches(self, catalogue) -> None:
        """'word' must not find 'Microsoft Edge' through the shared word."""
        names = [m.app.name for m in resolve("word", catalogue)]
        assert "Microsoft Edge" not in names
        assert "Microsoft Teams" not in names

    def test_matches_are_ranked_best_first(self, catalogue) -> None:
        matches = resolve("word", catalogue)
        assert matches[0].app.name == "Microsoft Word"
        assert matches[0].score >= matches[-1].score

    def test_every_match_explains_itself(self, catalogue) -> None:
        for match in resolve("chrome", catalogue):
            assert match.reason

    def test_empty_query_matches_nothing(self, catalogue) -> None:
        assert resolve("", catalogue) == []
        assert resolve("   the app  ", catalogue) == []

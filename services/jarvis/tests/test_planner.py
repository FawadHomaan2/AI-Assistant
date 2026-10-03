"""Planner: mapping a request to tool calls, or declining to guess."""

from __future__ import annotations

import pytest

from jarvis.agents.planner import plan


class TestMapped:
    @pytest.mark.parametrize(
        ("message", "operation"),
        [
            ("list my downloads", "list"),
            ("show me what is in my desktop", "list"),
            ("find my pdf files in documents", "search"),
            ("search for word documents in downloads", "search"),
            ("create a folder called University on my desktop", "create_folder"),
            ("find duplicate files in downloads", "find_duplicates"),
        ],
    )
    def test_filesystem_requests(self, message: str, operation: str) -> None:
        steps = plan(message).steps
        assert steps and steps[0].args["operation"] == operation

    def test_file_type_becomes_a_glob(self) -> None:
        args = plan("find my pdf files in documents").steps[0].args
        assert args["pattern"] == "*.pdf"

    def test_recency_becomes_a_date_filter(self) -> None:
        args = plan("find my pdf files from last month in documents").steps[0].args
        assert args["modified_within_days"] == 31

    def test_folder_name_is_extracted(self) -> None:
        args = plan("create a folder called University on my desktop").steps[0].args
        assert args["path"] == "desktop/University"

    def test_reading_a_named_document(self) -> None:
        steps = plan("read budget.csv").steps
        assert steps[0].tool == "document"
        assert steps[0].args["path"] == "budget.csv"

    # An explicit path goes to the tool so the jail gives its specific reason.
    @pytest.mark.parametrize("path", ["/etc/passwd", r"C:\Windows\System32\config"])
    def test_explicit_paths_reach_the_tool_layer(self, path: str) -> None:
        steps = plan(f"read {path}").steps
        assert steps and steps[0].tool == "document"
        assert steps[0].args["path"] == path


class TestDeclining:
    """Guessing here moves or deletes the wrong files."""

    def test_open_ended_requests_are_declined(self) -> None:
        result = plan("sort out my downloads however you think is best")
        assert result.steps == []
        assert "can't yet work out the exact steps" in result.unsupported

    @pytest.mark.parametrize(
        ("message", "phase_text"),
        [
            ("turn off bluetooth", "not built yet"),
            ("check my startup programs", "Phase 9"),
        ],
    )
    def test_other_domains_name_the_right_missing_capability(
        self, message: str, phase_text: str
    ) -> None:
        result = plan(message)
        assert result.steps == []
        assert phase_text in result.unsupported

    def test_the_decline_says_what_it_can_do(self) -> None:
        assert "list a folder" in plan("do something clever with my files").unsupported


class TestDeleting:
    """Deleting is mapped so the request reaches the gate, which refuses it by
    default — a silent "I don't understand" would hide that the capability
    exists but is switched off."""

    def test_delete_maps_to_the_recycle_bin_operation(self) -> None:
        steps = plan("delete report.pdf from downloads").steps
        assert steps[0].args["operation"] == "delete"
        assert steps[0].args["path"] == "downloads/report.pdf"

    def test_delete_defaults_to_downloads(self) -> None:
        assert plan("remove old.txt").steps[0].args["path"] == "downloads/old.txt"

    def test_it_never_maps_to_a_permanent_delete(self) -> None:
        """The planner must never choose the unrecoverable variant on its own."""
        for phrasing in ["delete report.pdf", "remove report.pdf from downloads"]:
            steps = plan(phrasing).steps
            assert steps[0].args["operation"] == "delete"


class TestApplicationsAndWindows:
    """Phase 4 mappings."""

    @pytest.mark.parametrize(
        ("message", "name"),
        [
            ("open chrome", "chrome"),
            ("launch spotify", "spotify"),
            ("start visual studio code", "visual studio code"),
            ("run notepad", "notepad"),
        ],
    )
    def test_launching(self, message: str, name: str) -> None:
        steps = plan(message).steps
        assert steps[0].tool == "application"
        assert steps[0].args == {"operation": "launch", "name": name}

    @pytest.mark.parametrize(
        ("message", "operation"),
        [
            ("close notepad", "close"),
            ("quit spotify", "close"),
            ("minimise word", "minimise"),
            ("maximize chrome", "maximise"),
            ("switch to chrome", "focus"),
        ],
    )
    def test_window_actions(self, message: str, operation: str) -> None:
        steps = plan(message).steps
        assert steps[0].tool == "window"
        assert steps[0].args["operation"] == operation

    # "open my downloads folder" is a file request, not an app launch.
    @pytest.mark.parametrize(
        "message",
        ["open my downloads folder", "open my documents folder", "show my desktop"],
    )
    def test_folder_requests_are_not_app_launches(self, message: str) -> None:
        steps = plan(message).steps
        assert steps[0].tool == "filesystem"
        assert steps[0].args["operation"] == "list"

    def test_opening_a_document_is_not_an_app_launch(self) -> None:
        steps = plan("open report.pdf").steps
        assert steps[0].tool == "document"

    @pytest.mark.parametrize(
        ("message", "operation"),
        [
            ("what is running", "list"),
            ("which programs are open", "list"),
            ("list my processes", "list"),
            ("what is using my cpu", "top"),
            ("what's hogging my memory", "top"),
        ],
    )
    def test_process_questions(self, message: str, operation: str) -> None:
        steps = plan(message).steps
        assert steps[0].tool == "process"
        assert steps[0].args["operation"] == operation

    def test_slowness_now_runs_real_diagnostics(self) -> None:
        """ "Why is it slow" is measured, not guessed at."""
        steps = plan("why is my laptop slow").steps
        assert steps[0].tool == "diagnostics"
        assert steps[0].args["operation"] == "performance"

    def test_screenshots_map_to_the_capture_tool(self) -> None:
        assert plan("take a screenshot").steps[0].tool == "screenshot"

    def test_driver_and_startup_checks_still_name_phase_9(self) -> None:
        """Those land with the Security Center, and the reply says so."""
        result = plan("check my startup programs")
        assert result.steps == []
        assert "Phase 9" in result.unsupported


class TestWeb:
    """Browsing and searching, added in Phase 7."""

    @pytest.mark.parametrize(
        ("message", "url"),
        [
            ("go to example.com", "example.com"),
            (
                "visit https://news.ycombinator.com/news?p=2",
                "https://news.ycombinator.com/news?p=2",
            ),
            ("open github.com", "github.com"),
            ("pull up wikipedia.org", "wikipedia.org"),
            ("navigate to example.com/pricing", "example.com/pricing"),
            ("browse to https://example.org.", "https://example.org"),
        ],
    )
    def test_addresses_go_to_the_browser(self, message: str, url: str) -> None:
        steps = plan(message).steps
        assert steps[0].tool == "browser"
        assert steps[0].args == {"operation": "open", "url": url}

    def test_a_domain_is_not_read_as_a_filename(self) -> None:
        """Regression: "github.com" matched the "name.ext" document pattern.

        The document branch saw a three-letter extension and tried to read
        "github.com" off the disk, where the path jail then refused it — a
        confusing answer to a perfectly clear request.
        """
        steps = plan("open github.com").steps
        assert steps[0].tool == "browser"

    def test_a_domain_is_not_read_as_an_application(self) -> None:
        assert plan("open example.com").steps[0].tool == "browser"

    @pytest.mark.parametrize(
        ("message", "query"),
        [
            ("search the web for best laptops 2026", "best laptops 2026"),
            ("search online for quiet keyboards", "quiet keyboards"),
            ("google the weather in Dhaka", "the weather in Dhaka"),
            ("look up rust async traits online", "rust async traits"),
            ("do a web search for tauri vs electron", "tauri vs electron"),
            ("search the internet for python 3.14 release notes", "python 3.14 release notes"),
        ],
    )
    def test_searches_go_to_the_search_tool(self, message: str, query: str) -> None:
        steps = plan(message).steps
        assert steps[0].tool == "websearch"
        assert steps[0].args == {"query": query}

    def test_a_search_with_no_terms_is_not_a_search(self) -> None:
        assert plan("search the web").steps == []

    def test_a_domain_mentioned_in_passing_is_not_a_request(self) -> None:
        """ "my email is me@example.com" names a domain but asks for nothing."""
        assert plan("my email is me@example.com").steps == []

    def test_file_requests_win_over_domain_lookalikes(self) -> None:
        """ "open my notes.io file" is a document, whatever ".io" looks like."""
        steps = plan("open my notes.io file").steps
        assert steps[0].tool == "document"

    def test_looking_for_files_is_not_a_web_search(self) -> None:
        """Regression: "look for" matched the web-search verb list.

        "look for my CV" is about this computer. Only "look up" is the web.
        """
        assert all(s.tool != "websearch" for s in plan("look for my CV").steps)

    def test_finding_files_still_searches_the_disk(self) -> None:
        steps = plan("find my pdfs in documents").steps
        assert steps[0].tool == "filesystem"
        assert steps[0].args["operation"] == "search"

    def test_web_browsing_is_no_longer_reported_as_unbuilt(self) -> None:
        """Phase 7 shipped it, so the "arrives in Phase 7" notice must be gone."""
        for message in ("go to example.com", "search the web for cats"):
            assert "Phase 7" not in plan(message).unsupported

"""Redaction is the last line of defence before a secret reaches a log."""

from __future__ import annotations

import pytest

from jarvis.util.redaction import MASK, redact, redact_text


@pytest.mark.parametrize(
    "raw",
    [
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "sk-proj-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "ghp_CCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    ],
)
def test_known_key_formats_are_masked(raw: str) -> None:
    assert raw not in redact_text(f"the key is {raw} ok")


def test_secret_keys_masked_by_name() -> None:
    out = redact(
        {
            "api_key": "whatever",
            "password": "hunter2",
            "nested": {"access_token": "xyz", "note": "fine"},
            "list": [{"client_secret": "s"}],
        }
    )
    assert out["api_key"] == MASK
    assert out["password"] == MASK
    assert out["nested"]["access_token"] == MASK
    assert out["nested"]["note"] == "fine"
    assert out["list"][0]["client_secret"] == MASK


def test_ordinary_text_survives() -> None:
    """Over-redaction is preferred, but it must not mangle normal messages."""
    for text in [
        "Create a folder called University on my Desktop",
        "C:\\Users\\me\\Downloads\\report.pdf",
        "why is my laptop slow",
        "Open Chrome and search for React docs",
    ]:
        assert redact_text(text) == text


def test_emails_are_masked() -> None:
    assert "someone@example.com" not in redact_text("mail someone@example.com now")


def test_structure_is_preserved() -> None:
    out = redact({"a": [1, 2, {"b": "c"}], "t": ("x", "y")})
    assert out["a"][2]["b"] == "c"
    assert isinstance(out["t"], tuple)


def test_recursion_is_depth_limited() -> None:
    """A cyclic structure must not hang the logger."""
    node: dict = {}
    node["self"] = node
    assert redact(node) is not None

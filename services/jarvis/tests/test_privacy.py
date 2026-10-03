"""The egress gate. These are the tests that matter most for privacy claims."""

from __future__ import annotations

import pytest

from jarvis.ai.privacy import MAX_EGRESS_CHARS, check, sanitise
from jarvis.ai.types import CompletionRequest, Message, PrivacyClass
from jarvis.util.errors import PrivacyViolation


def _req(privacy: PrivacyClass, text: str = "hello") -> CompletionRequest:
    return CompletionRequest(messages=[Message("user", text)], privacy=privacy)


ALL_PERMISSIVE = {"allow_cloud": True, "allow_cloud_content": True}


def test_sensitive_never_leaves_even_when_everything_is_enabled() -> None:
    """SENSITIVE is not a setting — it is a hard rule."""
    with pytest.raises(PrivacyViolation, match="never sent to a cloud provider"):
        check(
            _req(PrivacyClass.SENSITIVE),
            provider_name="anthropic",
            is_cloud=True,
            **ALL_PERMISSIVE,
        )


def test_sensitive_is_fine_locally() -> None:
    decision = check(
        _req(PrivacyClass.SENSITIVE),
        provider_name="ollama",
        is_cloud=False,
        allow_cloud=False,
        allow_cloud_content=False,
    )
    assert decision.allowed


def test_cloud_disabled_blocks_everything_cloud() -> None:
    with pytest.raises(PrivacyViolation, match="Cloud AI is turned off"):
        check(
            _req(PrivacyClass.PUBLIC),
            provider_name="anthropic",
            is_cloud=True,
            allow_cloud=False,
            allow_cloud_content=False,
        )


def test_content_needs_its_own_permission() -> None:
    with pytest.raises(PrivacyViolation, match="file or screen content"):
        check(
            _req(PrivacyClass.CONTENT),
            provider_name="anthropic",
            is_cloud=True,
            allow_cloud=True,
            allow_cloud_content=False,
        )


def test_metadata_allowed_when_cloud_enabled() -> None:
    assert check(
        _req(PrivacyClass.METADATA),
        provider_name="anthropic",
        is_cloud=True,
        allow_cloud=True,
        allow_cloud_content=False,
    ).allowed


def test_local_provider_is_never_blocked() -> None:
    for privacy in PrivacyClass:
        assert check(
            _req(privacy),
            provider_name="ollama",
            is_cloud=False,
            allow_cloud=False,
            allow_cloud_content=False,
        ).allowed


def test_sanitise_redacts_secrets() -> None:
    req = _req(PrivacyClass.PUBLIC, "my key is sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAA")
    out, removed = sanitise(req)
    assert "sk-ant" not in out.messages[0].content
    assert removed > 0


def test_sanitise_caps_payload_size() -> None:
    out, _ = sanitise(_req(PrivacyClass.PUBLIC, "x" * (MAX_EGRESS_CHARS + 5_000)))
    assert len(out.messages[0].content) <= MAX_EGRESS_CHARS + 100
    assert "truncated" in out.messages[0].content


def test_sanitise_does_not_mutate_the_original() -> None:
    req = _req(PrivacyClass.PUBLIC, "key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAA")
    original = req.messages[0].content
    sanitise(req)
    assert req.messages[0].content == original

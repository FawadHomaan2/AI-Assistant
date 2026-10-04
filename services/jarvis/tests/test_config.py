from __future__ import annotations

import pytest

from jarvis.config.settings import ProviderSettings, ServerSettings, Settings, load
from jarvis.util.errors import ConfigError


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com"])
def test_non_loopback_bind_is_refused(host: str) -> None:
    """The API drives the computer; it is never exposed off-host."""
    with pytest.raises(ValueError, match="loopback"):
        ServerSettings(host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_accepted(host: str) -> None:
    assert ServerSettings(host=host).host == host


def test_api_key_pasted_into_credential_field_is_rejected() -> None:
    """config.toml must never end up holding a key."""
    with pytest.raises(ValueError, match="NAME of a credential-store entry"):
        ProviderSettings(credential="sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAA")


def test_credential_name_accepted() -> None:
    assert ProviderSettings(credential="jarvis/anthropic").credential == "jarvis/anthropic"


def test_unknown_provider_raises_with_help(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="not configured"):
        settings.provider("nope")


def test_job_routing_falls_back_to_default(settings: Settings) -> None:
    name, _ = settings.provider_for_job("planner")
    assert name == "dev_echo"


def test_job_routing_uses_override(settings: Settings) -> None:
    settings.ai.providers["other"] = ProviderSettings(kind="dev_echo")
    settings.ai.jobs["planner"] = "other"
    assert settings.provider_for_job("planner")[0] == "other"
    assert settings.provider_for_job("chat")[0] == "dev_echo"


def test_invalid_toml_is_explained(tmp_path) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text("this is not = valid = toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(bad)


def test_missing_file_uses_defaults(tmp_path) -> None:
    assert load(tmp_path / "absent.toml").ai.default == "dev_echo"

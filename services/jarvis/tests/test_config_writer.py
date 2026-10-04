"""Editing config.toml from the running app.

The properties worth pinning are the ones whose failure is expensive rather
than annoying: an edit that deletes the file's explanations, an edit that
leaves a config the core cannot start from, and an edit that writes a secret
into a plaintext file.
"""

from __future__ import annotations

import re

import pytest

from jarvis.config import settings as settings_module
from jarvis.config import writer
from jarvis.util.errors import ConfigError


@pytest.fixture
def config(tmp_path):
    """A config.toml with the shipped contents, which is mostly comments."""
    path = tmp_path / "config.toml"
    path.write_text(settings_module.DEFAULT_CONFIG_TOML, encoding="utf-8")
    return path


# ── adding and changing a provider ───────────────────────────────────────────


def test_adds_a_provider_and_keeps_every_comment(config):
    before = config.read_text("utf-8")
    comments_before = [ln for ln in before.splitlines() if ln.startswith("#")]
    assert len(comments_before) > 20, "the shipped config is mostly explanation"

    result = writer.set_provider(
        "anthropic",
        kind="anthropic",
        model="claude-opus-5-5",
        credential="jarvis/anthropic",
        path=config,
    )

    assert result.ai.providers["anthropic"].kind == "anthropic"
    assert result.ai.providers["anthropic"].model == "claude-opus-5-5"
    assert result.ai.providers["anthropic"].credential == "jarvis/anthropic"

    after = config.read_text("utf-8")
    comments_after = [ln for ln in after.splitlines() if ln.startswith("#")]
    assert comments_after == comments_before, "an edit must not strip the comments"
    # The pre-existing provider is still there.
    assert result.ai.providers["dev_echo"].kind == "dev_echo"


def test_the_written_file_loads_back(config):
    writer.set_provider(
        "gemini", kind="google", model="gemini-2.0-flash", credential="jarvis/gemini", path=config
    )
    reloaded = settings_module.load(config)
    assert reloaded.ai.providers["gemini"].kind == "google"
    assert reloaded.ai.providers["gemini"].model == "gemini-2.0-flash"


def test_replacing_a_provider_does_not_duplicate_it(config):
    writer.set_provider("openai", kind="openai_compat", model="gpt-4o", path=config)
    writer.set_provider("openai", kind="openai_compat", model="gpt-5", path=config)
    result = settings_module.load(config)
    assert result.ai.providers["openai"].model == "gpt-5"
    # Counted at line starts only: the shipped config carries
    # `# [ai.providers.openai]` as a commented example, so a plain substring
    # count is 2 before anything has been written.
    blocks = re.findall(r"^\[ai\.providers\.openai\]", config.read_text("utf-8"), re.M)
    assert len(blocks) == 1


def test_empty_fields_are_left_out_rather_than_written_blank(config):
    writer.set_provider("ollama", kind="ollama", path=config)
    text = config.read_text("utf-8")
    assert "[ai.providers.ollama]" in text
    # A blank `model = ""` reads as a deliberate empty model rather than
    # "use the adapter's default", which is what omitting it means.
    section = text.split("[ai.providers.ollama]", 1)[1]
    assert 'model = ""' not in section
    assert 'credential = ""' not in section


def test_a_base_url_round_trips(config):
    writer.set_provider(
        "groq",
        kind="openai_compat",
        model="llama-3.3-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
        credential="jarvis/groq",
        path=config,
    )
    result = settings_module.load(config)
    assert result.ai.providers["groq"].base_url == "https://api.groq.com/openai/v1"


def test_works_when_the_file_is_missing(tmp_path):
    missing = tmp_path / "nested" / "config.toml"
    result = writer.set_provider("ollama", kind="ollama", path=missing)
    assert missing.exists()
    assert result.ai.providers["ollama"].kind == "ollama"
    # Rebuilt from the shipped file, so a deleted config comes back explained.
    assert "NEVER contains secrets" in missing.read_text("utf-8")


# ── what must be refused ─────────────────────────────────────────────────────


def test_refuses_an_api_key_pasted_into_the_credential_field(config):
    """`credential` is a name, not a secret. The config file is plaintext."""
    before = config.read_text("utf-8")
    with pytest.raises(ConfigError):
        writer.set_provider(
            "anthropic",
            kind="anthropic",
            credential="sk-ant-api03-not-a-real-key",
            path=config,
        )
    assert config.read_text("utf-8") == before, "a refused edit must change nothing"


@pytest.mark.parametrize(
    "name",
    [
        "",
        "has space",
        "-leading-hyphen",
        'quote"inside',
        "bracket]",
        "dot.separated",  # would nest a sub-table rather than name a provider
        "x" * 65,
    ],
)
def test_refuses_an_unusable_provider_name(config, name):
    before = config.read_text("utf-8")
    with pytest.raises(ConfigError):
        writer.set_provider(name, kind="ollama", path=config)
    assert config.read_text("utf-8") == before


@pytest.mark.parametrize("credential", ["has space", "semi;colon", 'quote"x', "../escape"])
def test_refuses_an_unusable_credential_name(config, credential):
    with pytest.raises(ConfigError):
        writer.set_provider("x", kind="ollama", credential=credential, path=config)


def test_refuses_an_unknown_kind(config):
    before = config.read_text("utf-8")
    with pytest.raises(ConfigError):
        writer.set_provider("mystery", kind="not_a_real_kind", path=config)
    assert config.read_text("utf-8") == before


def test_an_unparseable_file_is_reported_not_overwritten(tmp_path):
    broken = tmp_path / "config.toml"
    broken.write_text('executable_path = "C:\\Program Files\\x"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        writer.set_provider("ollama", kind="ollama", path=broken)
    assert "C:\\Program Files" in broken.read_text("utf-8"), "the original must survive"


def test_no_temporary_files_are_left_behind(config):
    writer.set_provider("ollama", kind="ollama", path=config)
    with pytest.raises(ConfigError):
        writer.set_provider("bad name", kind="ollama", path=config)
    leftovers = [p.name for p in config.parent.iterdir() if p.name != "config.toml"]
    assert leftovers == []


# ── removing a provider ──────────────────────────────────────────────────────


def test_removing_the_default_provider_moves_the_default(config):
    writer.set_provider("ollama", kind="ollama", path=config)
    writer.set_ai(default="ollama", path=config)
    assert settings_module.load(config).ai.default == "ollama"

    result = writer.remove_provider("ollama", path=config)
    # Leaving `default` pointing at nothing makes every later request fail with
    # "not configured", which is a dead end reached by deleting something else.
    assert result.ai.default != "ollama"
    assert result.ai.default in result.ai.providers


def test_removing_a_provider_clears_its_job_overrides(config, tmp_path):
    config.write_text(
        config.read_text("utf-8") + '\n[ai.jobs]\nplanner = "ollama"\nrouter = "dev_echo"\n',
        encoding="utf-8",
    )
    writer.set_provider("ollama", kind="ollama", path=config)
    assert settings_module.load(config).ai.jobs["planner"] == "ollama"

    result = writer.remove_provider("ollama", path=config)
    assert "planner" not in result.ai.jobs, "an override naming a gone provider fails on use"
    assert result.ai.jobs["router"] == "dev_echo", "unrelated overrides stay"


def test_removing_something_that_is_not_there_says_so(config):
    with pytest.raises(ConfigError, match="no provider"):
        writer.remove_provider("never-existed", path=config)


def test_removing_the_only_provider_is_refused(config):
    """Otherwise it reads as a no-op: the provider is still listed afterwards.

    An emptied `[ai.providers]` table is dropped from the document, which makes
    the key absent, which makes `AISettings` apply its default factory and put
    `dev_echo` back.
    """
    assert set(settings_module.load(config).ai.providers) == {"dev_echo"}
    with pytest.raises(ConfigError, match="only configured provider"):
        writer.remove_provider("dev_echo", path=config)
    assert set(settings_module.load(config).ai.providers) == {"dev_echo"}


def test_removing_it_once_another_exists_works(config):
    writer.set_provider("ollama", kind="ollama", path=config)
    result = writer.remove_provider("dev_echo", path=config)
    assert set(result.ai.providers) == {"ollama"}
    assert result.ai.default == "ollama"


# ── the [ai] section ─────────────────────────────────────────────────────────


def test_cloud_access_toggles(config):
    assert settings_module.load(config).ai.allow_cloud is False
    result = writer.set_ai(allow_cloud=True, allow_cloud_content=True, path=config)
    assert result.ai.allow_cloud is True
    assert result.ai.allow_cloud_content is True
    # Written as TOML booleans, not strings.
    assert "allow_cloud = true" in config.read_text("utf-8")

    result = writer.set_ai(allow_cloud=False, path=config)
    assert result.ai.allow_cloud is False
    assert result.ai.allow_cloud_content is True, "one flag at a time"


def test_defaulting_to_a_provider_that_does_not_exist_is_refused(config):
    before = config.read_text("utf-8")
    with pytest.raises(ConfigError, match="no such provider"):
        writer.set_ai(default="typo", path=config)
    assert config.read_text("utf-8") == before


def test_setting_the_default_keeps_the_comments(config):
    comments = [ln for ln in config.read_text("utf-8").splitlines() if ln.startswith("#")]
    writer.set_provider("ollama", kind="ollama", path=config)
    writer.set_ai(default="ollama", allow_cloud=True, path=config)
    assert [ln for ln in config.read_text("utf-8").splitlines() if ln.startswith("#")] == comments


def test_a_sequence_of_edits_stays_loadable(config):
    """The realistic path: add Claude, point at it, allow cloud, then swap."""
    writer.set_provider("anthropic", kind="anthropic", credential="jarvis/anthropic", path=config)
    writer.set_ai(default="anthropic", allow_cloud=True, path=config)
    writer.set_provider("gemini", kind="google", credential="jarvis/gemini", path=config)
    writer.set_ai(default="gemini", path=config)
    writer.remove_provider("anthropic", path=config)

    result = settings_module.load(config)
    assert result.ai.default == "gemini"
    assert result.ai.allow_cloud is True
    assert "anthropic" not in result.ai.providers
    assert set(result.ai.providers) == {"dev_echo", "gemini"}

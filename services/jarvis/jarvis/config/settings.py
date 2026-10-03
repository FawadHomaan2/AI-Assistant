"""Configuration.

Precedence, lowest to highest: defaults -> config.toml -> environment.

`config.toml` NEVER holds a secret. A provider that needs a key stores a
reference (`credential = "jarvis/anthropic"`) and the value lives in the OS
credential store. See `jarvis.config.secrets`.
"""

from __future__ import annotations

from typing import Any, Literal

import tomlkit
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jarvis.config import paths
from jarvis.util.errors import ConfigError

ProviderKind = Literal["dev_echo", "anthropic", "openai_compat", "ollama", "llama_cpp"]
AssistantMode = Literal["paused", "guarded", "assisted", "developer"]


class ProviderSettings(BaseModel):
    """One AI provider. `credential` names an entry in the OS credential store."""

    kind: ProviderKind = "dev_echo"
    model: str = ""
    base_url: str = ""
    credential: str = ""
    max_tokens: int = Field(default=2048, ge=1, le=200_000)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)

    @field_validator("base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("credential")
    @classmethod
    def _reject_inline_secret(cls, v: str) -> str:
        """Catch a key pasted where a credential *name* belongs.

        Without this, a user following a half-remembered convention could put
        their API key straight into a plaintext config file.
        """
        if v and (v.startswith(("sk-", "sk-ant-", "ghp_")) or len(v) > 100):
            raise ValueError(
                "`credential` must be the NAME of a credential-store entry "
                "(e.g. 'jarvis/anthropic'), not the key itself. "
                "Keys are never stored in config.toml."
            )
        return v


class AISettings(BaseModel):
    # The provider used when a job class has no specific override.
    default: str = "dev_echo"
    providers: dict[str, ProviderSettings] = Field(
        default_factory=lambda: {"dev_echo": ProviderSettings(kind="dev_echo")}
    )
    # Per-job overrides, e.g. {"planner": "anthropic", "router": "ollama"}.
    jobs: dict[str, str] = Field(default_factory=dict)
    allow_cloud: bool = False
    # Content (document text, screenshots, clipboard) may reach a cloud model
    # only when this is explicitly enabled as well as `allow_cloud`.
    allow_cloud_content: bool = False


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    # 0 asks the OS for a free port, which is then reported in the handshake.
    port: int = Field(default=0, ge=0, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        """Refuse to expose the core beyond this machine.

        The API can drive the computer. Binding it to a routable address would
        hand that to the local network, so this is not configurable.
        """
        if v not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError(
                f"host must be loopback (127.0.0.1 or ::1), got {v!r}. "
                "The Jarvis API controls this computer and is never exposed off-host."
            )
        return v


class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    to_file: bool = True
    json_console: bool = False


class VoiceSettings(BaseModel):
    enabled: bool = False
    wake_word: str = "hey_jarvis"
    push_to_talk: bool = True


class BrowserConfig(BaseModel):
    """Web browsing. Off-by-default in the ways that matter.

    `allowed_hosts` is the important one. Without it, a page Jarvis reads could
    tell the model to go somewhere else and paste what it just read — the
    classic prompt-injection exfiltration route. An allowlist makes that
    impossible rather than unlikely.
    """

    enabled: bool = True
    headless: bool = True
    #: Sites Jarvis may visit. The default search engine is included so web
    #: search works on a fresh install; nothing else is.
    allowed_hosts: list[str] = Field(default_factory=lambda: ["duckduckgo.com"])
    #: Turning this on removes the allowlist entirely. Named to be read twice.
    allow_any_host: bool = False
    #: Permit `localhost` and `127.x` — for a developer's own dev server. Off by
    #: default because it lets a web page reach services on this machine.
    allow_loopback: bool = False
    search_engine: Literal["duckduckgo", "bing", "startpage"] = "duckduckgo"
    #: An already-installed Chromium or Edge, instead of Playwright's download.
    executable_path: str = ""
    timeout_seconds: float = Field(default=20.0, gt=0, le=180)

    @field_validator("allowed_hosts")
    @classmethod
    def _clean_hosts(cls, v: list[str]) -> list[str]:
        """Accept what people actually type, store the host only.

        "https://example.com/" and "EXAMPLE.COM" both mean example.com. Keeping
        the raw string would silently fail to match.
        """
        out: list[str] = []
        for raw in v:
            host = (raw or "").strip().lower()
            if not host:
                continue
            host = host.split("://", 1)[-1].split("/", 1)[0].split("@")[-1]
            host = host.split(":")[0].strip(".")
            if host and host not in out:
                out.append(host)
        return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    mode: AssistantMode = "guarded"
    ai: AISettings = Field(default_factory=AISettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)

    def provider(self, name: str | None = None) -> tuple[str, ProviderSettings]:
        """Resolve a provider by name, falling back to the default."""
        key = name or self.ai.default
        if key not in self.ai.providers:
            raise ConfigError(
                f"Provider {key!r} is not configured. "
                f"Known providers: {sorted(self.ai.providers) or ['none']}."
            )
        return key, self.ai.providers[key]

    def provider_for_job(self, job: str) -> tuple[str, ProviderSettings]:
        """Provider for a job class (router/planner/chat/...), else the default."""
        return self.provider(self.ai.jobs.get(job))


def load(path: Any = None) -> Settings:
    """Read config.toml if present, then let the environment override it."""
    file = paths.config_file() if path is None else path
    data: dict[str, Any] = {}
    try:
        if file.exists():
            data = dict(tomlkit.parse(file.read_text("utf-8")))
    except OSError as exc:
        raise ConfigError(f"Could not read {file}: {exc}") from exc
    except Exception as exc:  # tomlkit raises a family of parse errors
        raise ConfigError(f"{file} is not valid TOML: {exc}") from exc

    try:
        return Settings(**data)
    except Exception as exc:
        raise ConfigError(f"Invalid configuration in {file}: {exc}") from exc


DEFAULT_CONFIG_TOML = """\
# Jarvis configuration.
#
# This file NEVER contains secrets. A provider that needs an API key stores the
# NAME of a credential-store entry; the key itself lives in the Windows
# Credential Manager (DPAPI-protected, per-user).

mode = "guarded"

[server]
host = "127.0.0.1"
port = 0          # 0 = ask the OS for a free port

[logging]
level = "INFO"
to_file = true

[ai]
default = "dev_echo"
allow_cloud = false
allow_cloud_content = false

# Development echo provider. Not a language model: it reflects the request back
# so the transport and streaming path can be exercised without one.
[ai.providers.dev_echo]
kind = "dev_echo"

# [ai.providers.anthropic]
# kind = "anthropic"
# model = "claude-sonnet-4-5"
# credential = "jarvis/anthropic"   # name of the credential entry, not the key

# [ai.providers.ollama]
# kind = "ollama"
# model = "qwen2.5:14b-instruct"
# base_url = "http://127.0.0.1:11434"

# [ai.providers.local]
# kind = "openai_compat"
# model = "local-model"
# base_url = "http://127.0.0.1:1234/v1"   # LM Studio, vLLM, llama.cpp server
# credential = "jarvis/local"             # omit if the endpoint needs no key

[voice]
enabled = false
wake_word = "hey_jarvis"

[browser]
enabled = true
headless = true
search_engine = "duckduckgo"

# Sites Jarvis may visit. This list is what stops a page it reads from aiming
# it somewhere else, so keep it short and deliberate.
allowed_hosts = ["duckduckgo.com"]

# Removes the allowlist above. Only turn this on if you understand that a web
# page Jarvis reads can then direct it to any site on the internet.
allow_any_host = false

# Allows localhost and 127.0.0.1 — useful for your own dev server, and a way
# for a web page to reach services on this machine. Off unless you need it.
allow_loopback = false

# Point at an installed Chromium or Edge to skip Playwright's own download:
# executable_path = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
"""


def write_default_config(path: Any = None) -> Any:
    """Create config.toml if it is missing. Returns the path."""
    file = paths.config_file() if path is None else path
    if not file.exists():
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return file

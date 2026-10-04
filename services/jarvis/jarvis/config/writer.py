"""Changing `config.toml` from the running app.

Settings were read-only until now, which left the cloud providers unreachable:
`config.toml` ships them commented out, so a fresh install lists `dev_echo` and
nothing else, and the adapters' "add a key in Settings" pointed at a control
that did not exist.

Three properties this module is built around, each because the alternative
breaks something that is hard to recover from:

**Comments survive.** The shipped config is mostly explanation — which hosts
the browser may visit and why, what `allow_cloud` means. A `json`-style
load-and-dump would delete all of it the first time someone saved a setting,
so edits go through `tomlkit`, which keeps the document as written.

**A write that would not load back is refused.** The file is parsed and fed
through `Settings` before it replaces anything. An invalid `config.toml` stops
the core from starting at all, and a setting saved from the interface is
exactly where that must not happen.

**The replace is atomic.** Written to a temporary file in the same directory
and moved over the original, so an interruption leaves either the old file or
the new one, never half of either.

Secrets are not written here. `credential` holds the *name* of a credential
store entry; the value goes to the OS keychain through `jarvis.config.secrets`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import tomlkit

from jarvis.config import paths
from jarvis.config import settings as settings_module
from jarvis.config.settings import Settings
from jarvis.util.errors import ConfigError
from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: Provider and credential names become TOML keys and keychain entry names.
#: Restricted rather than quoted: a name needing quotes is a name that will be
#: mistyped, and this way neither the document nor the keychain can be steered
#: by what someone types in a text field.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SAFE_CREDENTIAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_/-]{0,127}$")


def check_name(name: str) -> str:
    if not SAFE_NAME.match(name or ""):
        raise ConfigError(
            f"{name!r} is not a usable provider name. Use letters, digits, "
            "hyphens and underscores, starting with a letter or digit."
        )
    return name


def check_credential(name: str) -> str:
    if name and not SAFE_CREDENTIAL.match(name):
        raise ConfigError(
            f"{name!r} is not a usable credential name. Use letters, digits, "
            "hyphens, underscores and slashes, e.g. 'jarvis/anthropic'."
        )
    return name


def _document(path: Path) -> tomlkit.TOMLDocument:
    """The config as a document that remembers its own formatting."""
    if not path.exists():
        # Start from the shipped file rather than an empty document, so a config
        # that was deleted comes back with its explanations intact.
        return tomlkit.parse(settings_module.DEFAULT_CONFIG_TOML)
    try:
        return tomlkit.parse(path.read_text("utf-8"))
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    except Exception as exc:  # tomlkit raises a family of parse errors
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def _save(path: Path, doc: tomlkit.TOMLDocument) -> Settings:
    """Validate, then replace the file atomically. Returns the new settings.

    Validation first and separately: `Settings(**data)` rejects things TOML is
    perfectly happy with — a loopback-only host, a key pasted where a
    credential name belongs — and finding that out after overwriting the file
    would mean the core no longer starts.
    """
    text = tomlkit.dumps(doc)

    try:
        parsed = dict(tomlkit.parse(text))
    except Exception as exc:
        raise ConfigError(f"The edit produced invalid TOML: {exc}") from exc
    try:
        validated = Settings(**parsed)
    except Exception as exc:
        raise ConfigError(f"The edit produced an invalid configuration: {exc}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    # Beside the original rather than in a temp directory: `os.replace` is only
    # atomic within one filesystem, and the config directory is the one place
    # guaranteed to be on the same one as the file being replaced.
    scratch = path.with_name(path.name + ".tmp")
    try:
        with scratch.open("w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(scratch, path)
    except OSError as exc:
        scratch.unlink(missing_ok=True)
        raise ConfigError(f"Could not write {path}: {exc}") from exc

    log.info("configuration updated", path=str(path))
    return validated


def _providers_table(doc: tomlkit.TOMLDocument) -> Any:
    """`[ai.providers]`, created if the document has no `[ai]` section yet."""
    if "ai" not in doc:
        doc["ai"] = tomlkit.table()
    ai = doc["ai"]
    if "providers" not in ai:
        ai["providers"] = tomlkit.table(is_super_table=True)
    return ai["providers"]


def set_provider(
    name: str,
    *,
    kind: str,
    model: str = "",
    base_url: str = "",
    credential: str = "",
    path: Path | None = None,
) -> Settings:
    """Add or replace one `[ai.providers.<name>]` block.

    Only the fields the interface collects. `max_tokens`, `temperature` and
    `timeout_seconds` keep their defaults unless someone sets them in the file
    by hand, which is deliberate: offering six numbers to fill in is how a
    settings panel stops being used.
    """
    check_name(name)
    check_credential(credential)
    config = paths.config_file() if path is None else path
    doc = _document(config)

    providers = _providers_table(doc)
    block = tomlkit.table()
    block["kind"] = kind
    if model:
        block["model"] = model
    if base_url:
        block["base_url"] = base_url
    if credential:
        block["credential"] = credential
    providers[name] = block

    return _save(config, doc)


def remove_provider(name: str, path: Path | None = None) -> Settings:
    """Delete a provider block. Removing the default falls back to dev_echo."""
    check_name(name)
    config = paths.config_file() if path is None else path
    doc = _document(config)

    providers = _providers_table(doc)
    if name not in providers:
        raise ConfigError(f"There is no provider {name!r} in {config}.")
    if len(providers) == 1:
        # Refused rather than allowed to look like a no-op. An empty
        # `[ai.providers]` table disappears from the document entirely, which
        # makes the key absent, which makes `AISettings` fall back to its
        # default factory and put `dev_echo` straight back — so the provider
        # the user just deleted is still listed afterwards.
        raise ConfigError(
            f"{name!r} is the only configured provider, so removing it would leave "
            "nothing to think with. Add another first, then remove this one."
        )
    del providers[name]

    # Leaving `default` pointing at a provider that no longer exists makes every
    # subsequent request fail with "not configured" — a dead end reached by
    # deleting something unrelated to the one in use.
    ai = doc["ai"]
    if str(ai.get("default", "")) == name:
        fallback = next((key for key in providers if key != name), "dev_echo")
        ai["default"] = fallback
        log.info("default provider removed; falling back", fallback=fallback)

    # Same for per-job overrides.
    jobs = ai.get("jobs")
    if jobs is not None:
        for job in [key for key, value in jobs.items() if str(value) == name]:
            del jobs[job]

    return _save(config, doc)


def set_ai(
    *,
    default: str | None = None,
    allow_cloud: bool | None = None,
    allow_cloud_content: bool | None = None,
    path: Path | None = None,
) -> Settings:
    """Change `[ai]`: which provider is used, and whether cloud models may be."""
    config = paths.config_file() if path is None else path
    doc = _document(config)

    if "ai" not in doc:
        doc["ai"] = tomlkit.table()
    ai = doc["ai"]

    if default is not None:
        check_name(default)
        # Checked here rather than left to `Settings`, which accepts a default
        # naming a provider that does not exist and only fails on use.
        providers = _providers_table(doc)
        if default not in providers:
            raise ConfigError(
                f"Cannot default to {default!r}: there is no such provider. "
                f"Configured: {sorted(providers) or ['none']}."
            )
        ai["default"] = default
    if allow_cloud is not None:
        ai["allow_cloud"] = allow_cloud
    if allow_cloud_content is not None:
        ai["allow_cloud_content"] = allow_cloud_content

    return _save(config, doc)

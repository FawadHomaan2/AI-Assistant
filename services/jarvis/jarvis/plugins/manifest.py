"""Plugin manifests.

A manifest is a plugin's declaration of what it is and what it needs. It is
also the only thing the host trusts from a plugin: everything the plugin says
at *runtime* is checked against what it said here, so a plugin cannot ask for a
capability it did not declare, and the user can read the full extent of what it
may do before enabling it.

The validation below is deliberately strict and deliberately boring. Every rule
exists because the alternative is a plugin doing something the user could not
have predicted from the manifest they approved:

  - **No critical-risk tools.** ARCHITECTURE §7: tier 5 is never plugin-exposed.
    A plugin that could register "format a disk" would be a worse attack surface
    than everything else in Jarvis combined.
  - **Known scopes only.** An unrecognised scope is refused rather than ignored.
    Ignoring it would mean a manifest that reads as asking for more than the
    host enforces.
  - **Names are constrained.** A plugin tool named `filesystem` would shadow a
    built-in in the registry; one named `../../etc` would be a path.
  - **The entry point stays inside the plugin's own folder.** It is a module
    path, not a filesystem path, and traversal out of the folder is refused.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.governance.risk import Risk
from jarvis.governance.scopes import DESCRIPTIONS, Scope
from jarvis.util.errors import JarvisError

MANIFEST_NAME = "plugin.json"

#: Lowercase, hyphenated, no path characters.
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,38}[a-z0-9]$")
_VERSION = re.compile(r"^\d+\.\d+(\.\d+)?$")
#: A dotted Python module path, relative to the plugin folder.
_ENTRY = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")

#: Tool names a plugin may never take, because the registry resolves by name
#: and shadowing a built-in would silently replace it.
RESERVED_TOOL_NAMES = frozenset(
    {
        "filesystem",
        "document",
        "process",
        "application",
        "window",
        "systeminfo",
        "network",
        "diagnostics",
        "screenshot",
        "clipboard",
        "notify",
        "powershell",
        "security",
        "browser",
        "websearch",
    }
)

#: The highest tier a plugin tool may declare.
MAX_PLUGIN_RISK = Risk.HIGH


class ManifestInvalid(JarvisError):
    code = "jarvis.plugin.manifest_invalid"
    http_status = 422


@dataclass(frozen=True)
class PluginTool:
    """One tool a plugin offers."""

    name: str
    description: str
    risk: Risk
    scopes: list[Scope]
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk.label,
            "scopes": [s.value for s in self.scopes],
            "inputSchema": self.input_schema,
        }


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    description: str
    author: str
    entry: str
    tools: list[PluginTool]
    #: The union of every tool's scopes. The host grants exactly this and no
    #: more, so the user sees one list rather than having to add them up.
    scopes: list[Scope]
    directory: Path | None = None

    @property
    def max_risk(self) -> Risk:
        return max((t.risk for t in self.tools), default=Risk.SAFE)

    def describe_scopes(self) -> list[dict[str, str]]:
        return [
            {"scope": s.value, "description": DESCRIPTIONS.get(s, s.value)} for s in self.scopes
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "entry": self.entry,
            "tools": [t.to_dict() for t in self.tools],
            "scopes": [s.value for s in self.scopes],
            "scopeDetail": self.describe_scopes(),
            "maxRisk": self.max_risk.label,
            "directory": str(self.directory) if self.directory else "",
        }


def _require(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestInvalid(f"A plugin manifest needs a {key!r}.")
    return value.strip()


def parse(data: dict[str, Any], directory: Path | None = None) -> Manifest:
    """Validate a manifest. Raises `ManifestInvalid` with a reason a human can act on."""
    name = _require(data, "name")
    if not _NAME.match(name):
        raise ManifestInvalid(
            f"{name!r} is not a usable plugin name. Use lowercase letters, digits "
            f"and hyphens, 2 to 40 characters — it is used as a folder name."
        )

    version = _require(data, "version")
    if not _VERSION.match(version):
        raise ManifestInvalid(f"{version!r} is not a version number like '1.0' or '1.2.3'.")

    entry = _require(data, "entry")
    if entry.endswith(".py"):
        # "main.py" is a *valid* dotted path — package `main`, module `py` — so
        # the pattern below accepts it and the import then fails confusingly.
        # Naming the mistake is far more useful than letting it through.
        raise ManifestInvalid(
            f"{entry!r} looks like a filename. The entry point is a module name, "
            f"so drop the .py: use {entry[:-3]!r}."
        )
    if not _ENTRY.match(entry):
        raise ManifestInvalid(
            f"{entry!r} is not a module name. The entry point is a Python module "
            f"inside the plugin's own folder, like 'main' or 'jarvis_plugin.run' "
            f"— not a file path."
        )

    raw_tools = data.get("tools")
    if not isinstance(raw_tools, list) or not raw_tools:
        raise ManifestInvalid("A plugin must declare at least one tool.")

    tools: list[PluginTool] = []
    seen: set[str] = set()
    for entry_data in raw_tools:
        tools.append(_parse_tool(entry_data, seen))

    scopes = sorted({s for t in tools for s in t.scopes}, key=lambda s: s.value)
    return Manifest(
        name=name,
        version=version,
        description=str(data.get("description", "")).strip(),
        author=str(data.get("author", "")).strip(),
        entry=entry,
        tools=tools,
        scopes=scopes,
        directory=directory,
    )


def _parse_tool(raw: Any, seen: set[str]) -> PluginTool:
    if not isinstance(raw, dict):
        raise ManifestInvalid("Each tool in a manifest must be an object.")

    name = _require(raw, "name")
    if not _TOOL_NAME.match(name):
        raise ManifestInvalid(
            f"{name!r} is not a usable tool name. Use lowercase letters, digits and underscores."
        )
    if name in RESERVED_TOOL_NAMES:
        raise ManifestInvalid(
            f"{name!r} is a built-in tool. A plugin cannot take that name, because "
            f"it would silently replace the built-in one."
        )
    if name in seen:
        raise ManifestInvalid(f"The tool {name!r} is declared twice.")
    seen.add(name)

    risk_label = str(raw.get("risk", "")).strip().lower()
    risk = {r.label: r for r in Risk}.get(risk_label)
    if risk is None:
        raise ManifestInvalid(
            f"{name!r} declares risk {risk_label!r}. Expected one of: "
            f"{', '.join(r.label for r in Risk)}."
        )
    if risk > MAX_PLUGIN_RISK:
        raise ManifestInvalid(
            f"{name!r} declares {risk.label} risk. Plugins may not go above "
            f"{MAX_PLUGIN_RISK.label}: a critical action is never exposed through "
            f"a plugin, whatever it claims to do."
        )

    raw_scopes = raw.get("scopes", [])
    if not isinstance(raw_scopes, list):
        raise ManifestInvalid(f"{name!r} must declare its scopes as a list.")
    scopes: list[Scope] = []
    for item in raw_scopes:
        try:
            scopes.append(Scope(str(item)))
        except ValueError as exc:
            raise ManifestInvalid(
                f"{name!r} asks for an unknown permission {item!r}. Jarvis refuses "
                f"the manifest rather than ignoring it — a permission the host "
                f"does not understand is one it cannot enforce."
            ) from exc

    schema = raw.get("inputSchema", {})
    if not isinstance(schema, dict):
        raise ManifestInvalid(f"{name!r} has an inputSchema that is not an object.")

    return PluginTool(
        name=name,
        description=str(raw.get("description", "")).strip(),
        risk=risk,
        scopes=scopes,
        input_schema=schema,
    )


def load(directory: Path) -> Manifest:
    """Read and validate `plugin.json` from a plugin folder."""
    path = directory / MANIFEST_NAME
    if not path.is_file():
        raise ManifestInvalid(f"{directory.name} has no {MANIFEST_NAME}, so it is not a plugin.")
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestInvalid(f"{path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ManifestInvalid(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestInvalid(f"{path} must contain a JSON object.")
    return parse(data, directory)

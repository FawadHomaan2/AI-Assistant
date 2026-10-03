"""The plugin host: discovery, enabling, and the scope ceiling.

Three rules, and the third is the one the phase gate is about.

**Off by default.** A discovered plugin is listed and not loaded. Installing
something must never be the same act as running it.

**Enabling is a permission decision, not a convenience.** Enabling shows the
manifest's full scope list, and the host records what was approved. A plugin
that later wants more has to be approved again — its manifest changed, so the
thing the user agreed to no longer exists.

**A plugin's effective scopes are the intersection of three sets**: what it
declared, what the user approved for it, and what the user has granted Jarvis
itself. A plugin can never exceed any of them, and the narrowest wins. The
third is easy to forget and matters most: if the user revokes `fs.read` from
Jarvis, every plugin loses it too, because a plugin acting through Jarvis
cannot have more reach than Jarvis has.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.db.engine import Database
from jarvis.governance.scopes import Scope, ScopeGrants
from jarvis.plugins.manifest import Manifest, ManifestInvalid
from jarvis.plugins.manifest import load as load_manifest
from jarvis.plugins.sandbox import PluginProcess
from jarvis.util.logging import get_logger

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class InstalledPlugin:
    manifest: Manifest
    enabled: bool = False
    #: The scopes the user approved when they enabled it. Kept separately from
    #: the manifest so an update that widens the request is detectable.
    approved_scopes: list[Scope] = field(default_factory=list)
    installed_at: str = ""
    #: Set when the folder is present but unusable, with the reason.
    error: str = ""
    process: PluginProcess | None = None

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def needs_reapproval(self) -> bool:
        """True when the manifest now asks for more than was approved."""
        return bool(set(self.manifest.scopes) - set(self.approved_scopes))

    def effective_scopes(self, grants: ScopeGrants) -> set[Scope]:
        """Declared ∩ approved ∩ granted-to-Jarvis. The narrowest wins."""
        return set(self.manifest.scopes) & set(self.approved_scopes) & grants.granted

    def to_dict(self, grants: ScopeGrants | None = None) -> dict[str, Any]:
        effective = sorted(s.value for s in (self.effective_scopes(grants) if grants else set()))
        return {
            **self.manifest.to_dict(),
            "enabled": self.enabled,
            "approvedScopes": [s.value for s in self.approved_scopes],
            "effectiveScopes": effective,
            "needsReapproval": self.needs_reapproval,
            "installedAt": self.installed_at,
            "error": self.error,
            "running": bool(self.process and self.process.running),
        }


class PluginHost:
    def __init__(self, db: Database, directory: Path) -> None:
        self.db = db
        self.directory = directory
        self.plugins: dict[str, InstalledPlugin] = {}

    # ── discovery ────────────────────────────────────────────────────────
    def discover(self) -> list[InstalledPlugin]:
        """Read every plugin folder. A bad manifest is listed with its reason.

        Skipping an invalid plugin silently would leave the user wondering why
        the thing they installed does nothing.
        """
        self.plugins = {}
        if not self.directory.is_dir():
            return []

        stored = self._stored()
        for child in sorted(self.directory.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                manifest = load_manifest(child)
            except ManifestInvalid as exc:
                log.warning("plugin manifest refused", folder=child.name, reason=exc.message)
                self.plugins[child.name] = _broken(child.name, exc.message)
                continue

            record = stored.get(manifest.name, {})
            self.plugins[manifest.name] = InstalledPlugin(
                manifest=manifest,
                enabled=bool(record.get("enabled", False)),
                approved_scopes=[
                    Scope(s) for s in record.get("approved", []) if s in {x.value for x in Scope}
                ],
                installed_at=str(record.get("installed_at") or _now()),
            )
        log.info("plugins discovered", count=len(self.plugins))
        return list(self.plugins.values())

    def _stored(self) -> dict[str, dict[str, Any]]:
        rows = self.db.query("SELECT * FROM plugins")
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                scopes = json.loads(row["scopes_json"])
            except (json.JSONDecodeError, TypeError):
                scopes = []
            out[row["name"]] = {
                "enabled": bool(row["enabled"]),
                "approved": scopes if isinstance(scopes, list) else [],
                "installed_at": row["installed_at"],
            }
        return out

    # ── enabling ─────────────────────────────────────────────────────────
    def enable(self, name: str) -> InstalledPlugin:
        """Enable a plugin, recording exactly which scopes were approved."""
        plugin = self.get(name)
        if plugin.error:
            raise ManifestInvalid(f"{name} cannot be enabled: {plugin.error}")
        plugin.enabled = True
        plugin.approved_scopes = list(plugin.manifest.scopes)
        self._persist(plugin)
        log.info(
            "plugin enabled",
            plugin=name,
            scopes=[s.value for s in plugin.approved_scopes],
        )
        return plugin

    async def disable(self, name: str) -> InstalledPlugin:
        plugin = self.get(name)
        plugin.enabled = False
        if plugin.process is not None:
            await plugin.process.stop()
            plugin.process = None
        self._persist(plugin)
        log.info("plugin disabled", plugin=name)
        return plugin

    def get(self, name: str) -> InstalledPlugin:
        plugin = self.plugins.get(name)
        if plugin is None:
            known = ", ".join(sorted(self.plugins)) or "none"
            raise ManifestInvalid(f"There is no plugin called {name!r}. Installed: {known}.")
        return plugin

    def _persist(self, plugin: InstalledPlugin) -> None:
        self.db.execute(
            "INSERT INTO plugins (name, version, enabled, manifest_json, scopes_json,"
            " installed_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET version = excluded.version,"
            " enabled = excluded.enabled, manifest_json = excluded.manifest_json,"
            " scopes_json = excluded.scopes_json",
            (
                plugin.name,
                plugin.manifest.version,
                int(plugin.enabled),
                json.dumps(plugin.manifest.to_dict(), default=str),
                json.dumps([s.value for s in plugin.approved_scopes]),
                plugin.installed_at or _now(),
            ),
        )

    # ── running ──────────────────────────────────────────────────────────
    def process_for(self, plugin: InstalledPlugin) -> PluginProcess:
        if plugin.process is None:
            assert plugin.manifest.directory is not None
            plugin.process = PluginProcess(
                plugin.name, plugin.manifest.directory, plugin.manifest.entry
            )
        return plugin.process

    async def stop_all(self) -> None:
        for plugin in self.plugins.values():
            if plugin.process is not None:
                await plugin.process.stop()
                plugin.process = None

    def enabled_plugins(self) -> list[InstalledPlugin]:
        return [p for p in self.plugins.values() if p.enabled and not p.error]

    def describe(self, grants: ScopeGrants) -> list[dict[str, Any]]:
        return [p.to_dict(grants) for p in self.plugins.values()]


def _broken(folder: str, reason: str) -> InstalledPlugin:
    """A placeholder so an unusable plugin is visible rather than absent."""
    return InstalledPlugin(
        manifest=Manifest(
            name=folder,
            version="0.0",
            description="",
            author="",
            entry="",
            tools=[],
            scopes=[],
        ),
        enabled=False,
        error=reason,
    )

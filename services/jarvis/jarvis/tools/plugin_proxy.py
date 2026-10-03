"""PluginToolProxy — a plugin's tool, behind the same gate as everything else.

A plugin does not get its own execution path. Its tools are registered in the
ordinary registry, previewed and executed by the ordinary executor, and judged
by the ordinary policy engine. That is the point: there is one gate, and adding
plugins must not add a second way around it.

What this class adds on top is the plugin's own ceiling, enforced in two places
because they fail at different times:

  **At registration**, the manifest's risk tier is capped and critical is
  refused outright, so a plugin cannot declare a tier-5 tool at all.

  **At every call**, the scopes required are re-derived from the manifest and
  intersected with what the user approved and what Jarvis itself holds. A
  plugin that edits its own manifest between calls, or one whose approval was
  narrowed while it was running, is checked against the narrower set — the
  check is not cached from when it was loaded.

The scopes are deliberately *declared by the manifest, not chosen by the
plugin at call time*. A plugin asking "please run this with fs.write" would be
a plugin choosing its own permissions.
"""

from __future__ import annotations

from typing import Any

from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope, ScopeGrants
from jarvis.plugins.host import InstalledPlugin, PluginHost
from jarvis.plugins.manifest import MAX_PLUGIN_RISK
from jarvis.plugins.manifest import PluginTool as ToolDeclaration
from jarvis.plugins.sandbox import PluginError
from jarvis.tools.base import Preview, Tool, ToolError, ToolInputInvalid, ToolResult, ToolSpec
from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: Plugin tool names are prefixed so they can never be confused with a built-in
#: in the registry, in the audit log, or in a consent prompt.
PREFIX = "plugin"


def qualified(plugin: str, tool: str) -> str:
    return f"{PREFIX}.{plugin}.{tool}"


class PluginScopeExceeded(ToolError):
    """A plugin asked for something outside its manifest or its approval."""

    code = "jarvis.plugin.scope_exceeded"
    http_status = 403


class PluginToolProxy(Tool):
    def __init__(
        self,
        host: PluginHost,
        plugin: InstalledPlugin,
        declaration: ToolDeclaration,
        grants: ScopeGrants,
    ) -> None:
        self.host = host
        self.plugin = plugin
        self.declaration = declaration
        self.grants = grants

    @property
    def spec(self) -> ToolSpec:
        available = self.plugin.enabled and not self.plugin.error
        return ToolSpec(
            name=qualified(self.plugin.name, self.declaration.name),
            description=(
                f"{self.declaration.description} "
                f"(from the {self.plugin.name} plugin, version {self.plugin.manifest.version})"
            ).strip(),
            scopes=list(self.declaration.scopes),
            # Capped rather than trusted. The manifest validator already refuses
            # critical, and this is the second place that would have to be
            # defeated for a plugin to claim a tier it may not have.
            risk=min(self.declaration.risk, MAX_PLUGIN_RISK),
            input_schema=self.declaration.input_schema or {"type": "object"},
            available=available,
            unavailable_reason=(
                self.plugin.error
                or ("" if available else f"The {self.plugin.name} plugin is turned off.")
            ),
        )

    def risk_for(self, args: dict[str, Any], preview: Preview) -> Risk:
        del args, preview
        return min(self.declaration.risk, MAX_PLUGIN_RISK)

    def scopes_for(self, args: dict[str, Any]) -> list[Scope]:
        """The manifest's scopes, not anything the call asked for.

        `args` is ignored on purpose: a plugin choosing its own permissions at
        call time is a plugin without permissions.
        """
        del args
        return list(self.declaration.scopes)

    def _ceiling_breach(self) -> str:
        """Why this call may not proceed, or an empty string.

        Re-derived on every call rather than cached, so narrowing an approval
        or revoking a scope from Jarvis takes effect immediately — including
        for a plugin that is already running.
        """
        if self.plugin.error:
            return self.plugin.error
        if not self.plugin.enabled:
            return (
                f"The {self.plugin.name} plugin is turned off. Enable it in Settings "
                f"if you want this to work."
            )
        if self.plugin.needs_reapproval:
            missing = sorted(
                s.value for s in set(self.plugin.manifest.scopes) - set(self.plugin.approved_scopes)
            )
            return (
                f"{self.plugin.name} now asks for permissions you have not approved "
                f"({', '.join(missing)}). It has been stopped until you look at it "
                f"again — the plugin you approved is not the plugin that is installed."
            )

        effective = self.plugin.effective_scopes(self.grants)
        if beyond := [s for s in self.declaration.scopes if s not in effective]:
            names = ", ".join(s.value for s in beyond)
            not_approved = [s for s in beyond if s not in self.plugin.approved_scopes]
            if not_approved:
                return f"{self.plugin.name} is not approved for: {names}."
            # The plugin is approved, but Jarvis itself no longer holds it.
            return (
                f"{self.plugin.name} needs {names}, which Jarvis itself does not have. "
                f"A plugin cannot reach further than Jarvis can."
            )
        return ""

    async def preview(self, args: dict[str, Any]) -> Preview:
        if breach := self._ceiling_breach():
            return Preview(
                summary=f"{self.plugin.name} cannot run this",
                blocked=breach,
                reversible="undoable",
                blast_radius="Nothing changes.",
            )
        if not isinstance(args, dict):
            raise ToolInputInvalid("Plugin arguments must be an object.")

        scopes = ", ".join(s.value for s in self.declaration.scopes) or "nothing"
        return Preview(
            summary=f"{self.declaration.name} — from the {self.plugin.name} plugin",
            affected=1,
            reversible="unknown",
            blast_radius=(
                f"Runs third-party code from the {self.plugin.name} plugin in its own "
                f"process. It may use: {scopes}. Jarvis cannot see what the plugin "
                f"does beyond what it reports back."
            ),
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        # Checked again here, not only in preview: preview and execute are
        # separate calls, and a scope revoked in between must take effect.
        if breach := self._ceiling_breach():
            raise PluginScopeExceeded(breach)

        process = self.host.process_for(self.plugin)
        try:
            response = await process.call(self.declaration.name, args)
        except PluginError:
            raise
        except Exception as exc:  # a plugin must not be able to crash the host
            raise PluginError(f"{self.plugin.name} failed: {exc}") from exc

        if not response.ok:
            return ToolResult(
                ok=False,
                summary=f"{self.plugin.name} could not do that",
                error=response.error or "The plugin reported a failure without saying why.",
                data={"plugin": self.plugin.name, "tool": self.declaration.name},
            )

        summary = str(response.data.get("summary") or f"{self.declaration.name} finished")
        log.info("plugin tool ran", plugin=self.plugin.name, tool=self.declaration.name)
        return ToolResult(
            ok=True,
            summary=summary,
            # Namespaced, so a plugin's output can never be mistaken for the
            # core's own in the activity log or in a later prompt.
            data={"plugin": self.plugin.name, "result": response.data},
            changes=[str(c) for c in response.data.get("changes", []) if isinstance(c, str)],
        )


def register_plugins(registry: Any, host: PluginHost, grants: ScopeGrants) -> int:
    """Put every enabled plugin's tools into the ordinary registry."""
    count = 0
    for plugin in host.enabled_plugins():
        for declaration in plugin.manifest.tools:
            name = qualified(plugin.name, declaration.name)
            if registry.has(name):
                continue
            registry.register(PluginToolProxy(host, plugin, declaration, grants))
            count += 1
    return count

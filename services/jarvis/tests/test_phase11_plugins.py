"""Plugins, and the ceiling they cannot get over.

The Phase 11 gate: **a plugin cannot exceed its declared scopes**. These tests
try to get past that from every direction — a manifest asking for too much, a
plugin asking for something at call time that its manifest did not declare, a
manifest edited after approval, an approval narrowed while the plugin runs, and
a scope revoked from Jarvis itself.

The reference plugin in `plugins/word-count` is a real subprocess here, so the
isolation claims are exercised rather than asserted.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis.db.engine import Database
from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope, ScopeGrants
from jarvis.plugins import manifest as mf
from jarvis.plugins.host import PluginHost
from jarvis.plugins.sandbox import ENV_ALLOWLIST, PluginError, PluginTimeout, child_environment
from jarvis.tools.plugin_proxy import (
    PluginScopeExceeded,
    PluginToolProxy,
    qualified,
    register_plugins,
)
from jarvis.tools.registry import ToolRegistry

REFERENCE = Path(__file__).resolve().parents[3] / "plugins"


def a_manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "test-plugin",
        "version": "1.0",
        "description": "a plugin",
        "author": "tests",
        "entry": "main",
        "tools": [
            {
                "name": "do_thing",
                "description": "does a thing",
                "risk": "low",
                "scopes": ["fs.read"],
            }
        ],
    }
    base.update(overrides)
    return base


# ── manifest validation ──────────────────────────────────────────────────
class TestManifest:
    def test_a_good_manifest_parses(self) -> None:
        parsed = mf.parse(a_manifest())
        assert parsed.name == "test-plugin"
        assert parsed.scopes == [Scope.FS_READ]
        assert parsed.max_risk is Risk.LOW

    def test_a_critical_tool_is_refused(self) -> None:
        """ARCHITECTURE §7: tier 5 is never plugin-exposed."""
        bad = a_manifest(tools=[{"name": "nuke", "risk": "critical", "scopes": []}])
        with pytest.raises(mf.ManifestInvalid, match="critical"):
            mf.parse(bad)

    def test_an_unknown_scope_is_refused_not_ignored(self) -> None:
        """A permission the host does not understand is one it cannot enforce."""
        bad = a_manifest(tools=[{"name": "reader", "risk": "safe", "scopes": ["fs.teleport"]}])
        with pytest.raises(mf.ManifestInvalid, match="unknown permission"):
            mf.parse(bad)

    @pytest.mark.parametrize("name", list(mf.RESERVED_TOOL_NAMES)[:5])
    def test_a_plugin_cannot_shadow_a_built_in(self, name: str) -> None:
        bad = a_manifest(tools=[{"name": name, "risk": "safe", "scopes": []}])
        with pytest.raises(mf.ManifestInvalid, match="built-in"):
            mf.parse(bad)

    @pytest.mark.parametrize(
        "entry",
        ["../../../etc/passwd", "/etc/passwd", "..\\..\\windows", "a/b", "C:\\x"],
    )
    def test_an_entry_point_cannot_be_a_path(self, entry: str) -> None:
        with pytest.raises(mf.ManifestInvalid, match="module name"):
            mf.parse(a_manifest(entry=entry))

    def test_a_filename_as_an_entry_point_names_the_mistake(self) -> None:
        """ "main.py" is a valid dotted path — package `main`, module `py` — so
        it slipped through and failed confusingly at import time instead."""
        with pytest.raises(mf.ManifestInvalid, match=r"drop the \.py"):
            mf.parse(a_manifest(entry="main.py"))

    @pytest.mark.parametrize("name", ["../evil", "Evil", "a", "with space", "x" * 60, "-bad"])
    def test_plugin_names_are_constrained(self, name: str) -> None:
        with pytest.raises(mf.ManifestInvalid):
            mf.parse(a_manifest(name=name))

    def test_a_plugin_must_declare_at_least_one_tool(self) -> None:
        with pytest.raises(mf.ManifestInvalid, match="at least one tool"):
            mf.parse(a_manifest(tools=[]))

    def test_a_duplicate_tool_name_is_refused(self) -> None:
        bad = a_manifest(
            tools=[
                {"name": "thing", "risk": "safe", "scopes": []},
                {"name": "thing", "risk": "high", "scopes": ["fs.delete"]},
            ]
        )
        with pytest.raises(mf.ManifestInvalid, match="twice"):
            mf.parse(bad)

    def test_the_manifests_scope_list_is_the_union_of_its_tools(self) -> None:
        """The user sees one list rather than having to add them up."""
        parsed = mf.parse(
            a_manifest(
                tools=[
                    {"name": "alpha", "risk": "safe", "scopes": ["fs.read"]},
                    {"name": "beta", "risk": "low", "scopes": ["browser.use", "fs.read"]},
                ]
            )
        )
        assert parsed.scopes == [Scope.BROWSER_USE, Scope.FS_READ]

    def test_a_folder_without_a_manifest_is_not_a_plugin(self, tmp_path: Path) -> None:
        with pytest.raises(mf.ManifestInvalid, match="not a plugin"):
            mf.load(tmp_path)

    def test_broken_json_says_so(self, tmp_path: Path) -> None:
        (tmp_path / "plugin.json").write_text("{not json")
        with pytest.raises(mf.ManifestInvalid, match="not valid JSON"):
            mf.load(tmp_path)


# ── isolation ────────────────────────────────────────────────────────────
class TestIsolation:
    def test_the_child_environment_is_built_from_an_allowlist(self, monkeypatch) -> None:
        """A denylist would leak anything nobody thought of, which over time is
        everything."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setenv("JARVIS_TOKEN", "bearer-secret")
        env = child_environment()
        assert "ANTHROPIC_API_KEY" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "JARVIS_TOKEN" not in env
        assert "secret" not in json.dumps(env)

    def test_only_allowlisted_names_survive(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        env = child_environment()
        extra = set(env) - set(ENV_ALLOWLIST) - {"PYTHONDONTWRITEBYTECODE", "JARVIS_PLUGIN"}
        assert extra == set()


# ── the real reference plugin ────────────────────────────────────────────
needs_reference = pytest.mark.skipif(
    not (REFERENCE / "word-count" / "plugin.json").exists(),
    reason="the reference plugin folder is not present",
)


@pytest.fixture
def host() -> PluginHost:
    h = PluginHost(Database(":memory:"), REFERENCE)
    h.discover()
    return h


def proxy_for(host: PluginHost, grants: ScopeGrants | None = None) -> PluginToolProxy:
    plugin = host.get("word-count")
    return PluginToolProxy(host, plugin, plugin.manifest.tools[0], grants or ScopeGrants())


@needs_reference
class TestReferencePlugin:
    def test_discovery_does_not_enable(self, host: PluginHost) -> None:
        """Installing something must never be the same act as running it."""
        assert host.get("word-count").enabled is False
        assert host.enabled_plugins() == []

    async def test_a_disabled_plugin_refuses_rather_than_running(self, host: PluginHost) -> None:
        proxy = proxy_for(host)
        preview = await proxy.preview({"text": "x"})
        assert "turned off" in preview.blocked
        with pytest.raises(PluginScopeExceeded, match="turned off"):
            await proxy.execute({"text": "x"})

    async def test_an_enabled_plugin_runs_in_its_own_process(self, host: PluginHost) -> None:
        host.enable("word-count")
        proxy = proxy_for(host)
        result = await proxy.execute({"text": "the quick brown fox"})
        assert result.ok
        assert result.data["result"]["words"] == 4
        assert result.data["plugin"] == "word-count"
        process = host.process_for(host.get("word-count"))
        assert process.running
        assert process._process is not None
        assert process._process.pid != os.getpid(), "a plugin must not run in the host process"
        await host.stop_all()

    async def test_a_plugin_error_is_reported_not_raised(self, host: PluginHost) -> None:
        host.enable("word-count")
        result = await proxy_for(host).execute({"text": ""})
        assert result.ok is False
        assert "No text" in result.error
        await host.stop_all()

    async def test_a_plugin_survives_its_own_error(self, host: PluginHost) -> None:
        """One bad request must not end the process and lose the next one."""
        host.enable("word-count")
        proxy = proxy_for(host)
        await proxy.execute({"text": ""})
        assert (await proxy.execute({"text": "still here"})).ok
        await host.stop_all()

    async def test_the_result_is_namespaced_under_the_plugin(self, host: PluginHost) -> None:
        """A plugin's output must never be mistaken for the core's own."""
        host.enable("word-count")
        result = await proxy_for(host).execute({"text": "hello"})
        assert set(result.data) == {"plugin", "result"}
        await host.stop_all()

    async def test_enabling_is_remembered(self, host: PluginHost) -> None:
        host.enable("word-count")
        again = PluginHost(host.db, REFERENCE)
        again.discover()
        assert again.get("word-count").enabled is True

    async def test_disabling_stops_the_process(self, host: PluginHost) -> None:
        host.enable("word-count")
        await proxy_for(host).execute({"text": "hello"})
        await host.disable("word-count")
        assert host.get("word-count").process is None


# ── the ceiling: the Phase 11 gate ───────────────────────────────────────
@needs_reference
class TestScopeCeiling:
    async def test_a_plugin_cannot_use_a_scope_it_did_not_declare(self, host: PluginHost) -> None:
        """The scopes come from the manifest, never from the call.

        A plugin choosing its own permissions at call time is a plugin without
        permissions, so the arguments are ignored entirely.
        """
        host.enable("word-count")
        proxy = proxy_for(host)
        asked = proxy.scopes_for({"scopes": ["fs.delete"], "elevate": True})
        assert asked == list(proxy.declaration.scopes)
        assert Scope.FS_DELETE not in asked

    async def test_a_plugin_cannot_exceed_what_the_user_approved(self, host: PluginHost) -> None:
        host.enable("word-count")
        plugin = host.get("word-count")
        # The plugin is updated and now wants more than was approved.
        plugin.manifest.tools[0].scopes.append(Scope.FS_DELETE)
        object.__setattr__(plugin.manifest, "scopes", [Scope.FS_DELETE])
        proxy = PluginToolProxy(host, plugin, plugin.manifest.tools[0], ScopeGrants())
        with pytest.raises(PluginScopeExceeded, match="not the plugin that is installed"):
            await proxy.execute({"text": "x"})

    async def test_a_plugin_cannot_exceed_what_jarvis_itself_holds(self, host: PluginHost) -> None:
        """Revoke a capability from Jarvis and every plugin loses it too."""
        host.enable("word-count")
        plugin = host.get("word-count")
        declaration = mf.PluginTool(
            name="reader", description="", risk=Risk.SAFE, scopes=[Scope.FS_READ]
        )
        plugin.manifest.tools.append(declaration)
        object.__setattr__(plugin.manifest, "scopes", [Scope.FS_READ])
        plugin.approved_scopes = [Scope.FS_READ]

        with_grant = PluginToolProxy(host, plugin, declaration, ScopeGrants())
        assert with_grant._ceiling_breach() == ""

        revoked = ScopeGrants(grants=[])
        without = PluginToolProxy(host, plugin, declaration, revoked)
        assert "Jarvis itself does not have" in without._ceiling_breach()

    async def test_the_ceiling_is_rechecked_on_every_call(self, host: PluginHost) -> None:
        """Preview and execute are separate calls; a revocation between them bites."""
        host.enable("word-count")
        grants = ScopeGrants()
        plugin = host.get("word-count")
        declaration = mf.PluginTool(
            name="reader", description="", risk=Risk.SAFE, scopes=[Scope.FS_READ]
        )
        plugin.manifest.tools.append(declaration)
        object.__setattr__(plugin.manifest, "scopes", [Scope.FS_READ])
        plugin.approved_scopes = [Scope.FS_READ]
        proxy = PluginToolProxy(host, plugin, declaration, grants)

        assert (await proxy.preview({})).blocked == ""
        grants.revoke(Scope.FS_READ)
        with pytest.raises(PluginScopeExceeded):
            await proxy.execute({})

    async def test_the_risk_tier_is_capped_at_registration(self, host: PluginHost) -> None:
        """Even a declaration built in code cannot claim tier 5."""
        plugin = host.get("word-count")
        host.enable("word-count")
        declaration = mf.PluginTool(name="sneaky", description="", risk=Risk.CRITICAL, scopes=[])
        proxy = PluginToolProxy(host, plugin, declaration, ScopeGrants())
        assert proxy.spec.risk is mf.MAX_PLUGIN_RISK
        assert proxy.risk_for({}, None) is mf.MAX_PLUGIN_RISK  # type: ignore[arg-type]

    async def test_plugin_tools_are_prefixed_in_the_registry(self, host: PluginHost) -> None:
        host.enable("word-count")
        registry = ToolRegistry()
        assert register_plugins(registry, host, ScopeGrants()) == 1
        assert registry.has(qualified("word-count", "word_count"))
        assert not registry.has("word_count")

    async def test_a_disabled_plugin_registers_nothing(self, host: PluginHost) -> None:
        registry = ToolRegistry()
        assert register_plugins(registry, host, ScopeGrants()) == 0
        assert len(registry) == 0


# ── badly behaved plugins ────────────────────────────────────────────────
def write_plugin(root: Path, name: str, body: str, manifest: dict[str, Any] | None = None) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.json").write_text(
        json.dumps(
            manifest
            or a_manifest(
                name=name, tools=[{"name": "go", "description": "", "risk": "safe", "scopes": []}]
            )
        )
    )
    (folder / "main.py").write_text(body)
    return folder


class TestMisbehaviour:
    @pytest.fixture
    def sandbox_root(self, tmp_path: Path) -> Path:
        return tmp_path / "plugins"

    async def test_a_hanging_plugin_is_killed(self, sandbox_root: Path) -> None:
        write_plugin(
            sandbox_root,
            "hanger",
            "import time, sys\nsys.stdin.readline()\ntime.sleep(300)\n",
        )
        host = PluginHost(Database(":memory:"), sandbox_root)
        host.discover()
        host.enable("hanger")
        plugin = host.get("hanger")
        proxy = PluginToolProxy(host, plugin, plugin.manifest.tools[0], ScopeGrants())
        import jarvis.plugins.sandbox as sandbox

        original = sandbox.CALL_TIMEOUT_SECONDS
        sandbox.CALL_TIMEOUT_SECONDS = 1.0
        try:
            with pytest.raises(PluginTimeout, match="did not answer"):
                await proxy.execute({})
        finally:
            sandbox.CALL_TIMEOUT_SECONDS = original
            await host.stop_all()

    async def test_a_plugin_that_exits_is_reported_with_its_own_error(
        self, sandbox_root: Path
    ) -> None:
        write_plugin(
            sandbox_root, "crasher", "import sys\nprint('boom', file=sys.stderr)\nsys.exit(1)\n"
        )
        host = PluginHost(Database(":memory:"), sandbox_root)
        host.discover()
        host.enable("crasher")
        plugin = host.get("crasher")
        proxy = PluginToolProxy(host, plugin, plugin.manifest.tools[0], ScopeGrants())
        with pytest.raises(PluginError) as exc:
            await proxy.execute({})
        assert "boom" in exc.value.message
        await host.stop_all()

    async def test_a_plugin_returning_junk_is_refused(self, sandbox_root: Path) -> None:
        write_plugin(
            sandbox_root,
            "junk",
            "import sys\n"
            "sys.stdin.readline()\n"
            "sys.stdout.write('not json\\n')\n"
            "sys.stdout.flush()\n",
        )
        host = PluginHost(Database(":memory:"), sandbox_root)
        host.discover()
        host.enable("junk")
        plugin = host.get("junk")
        proxy = PluginToolProxy(host, plugin, plugin.manifest.tools[0], ScopeGrants())
        with pytest.raises(PluginError, match="not JSON"):
            await proxy.execute({})
        await host.stop_all()

    async def test_a_plugin_cannot_read_the_hosts_secrets(
        self, sandbox_root: Path, monkeypatch
    ) -> None:
        """The property that matters most: no ambient credentials."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
        write_plugin(
            sandbox_root,
            "snooper",
            "import json, os, sys\n"
            "sys.stdin.readline()\n"
            "payload = {'ok': True, 'data': {'env': sorted(os.environ)}}\n"
            "sys.stdout.write(json.dumps(payload) + '\\n')\n"
            "sys.stdout.flush()\n",
        )
        host = PluginHost(Database(":memory:"), sandbox_root)
        host.discover()
        host.enable("snooper")
        plugin = host.get("snooper")
        proxy = PluginToolProxy(host, plugin, plugin.manifest.tools[0], ScopeGrants())
        result = await proxy.execute({})
        assert result.ok
        assert "ANTHROPIC_API_KEY" not in result.data["result"]["env"]
        await host.stop_all()

    async def test_an_oversized_reply_is_refused(self, sandbox_root: Path) -> None:
        write_plugin(
            sandbox_root,
            "flooder",
            "import json, sys\n"
            "sys.stdin.readline()\n"
            "sys.stdout.write(json.dumps({'ok': True, 'data': {'x': 'a' * 2_000_000}}) + '\\n')\n"
            "sys.stdout.flush()\n",
        )
        host = PluginHost(Database(":memory:"), sandbox_root)
        host.discover()
        host.enable("flooder")
        plugin = host.get("flooder")
        proxy = PluginToolProxy(host, plugin, plugin.manifest.tools[0], ScopeGrants())
        with pytest.raises(PluginError, match="refuses to load"):
            await proxy.execute({})
        await host.stop_all()

    def test_a_broken_manifest_is_listed_with_its_reason(self, sandbox_root: Path) -> None:
        """Skipping it silently would leave the user wondering why nothing happens."""
        folder = sandbox_root / "broken"
        folder.mkdir(parents=True)
        (folder / "plugin.json").write_text('{"name": "broken"}')
        host = PluginHost(Database(":memory:"), sandbox_root)
        found = host.discover()
        assert len(found) == 1
        assert found[0].error
        assert found[0].enabled is False

    def test_a_broken_plugin_cannot_be_enabled(self, sandbox_root: Path) -> None:
        folder = sandbox_root / "broken"
        folder.mkdir(parents=True)
        (folder / "plugin.json").write_text('{"name": "broken"}')
        host = PluginHost(Database(":memory:"), sandbox_root)
        host.discover()
        with pytest.raises(mf.ManifestInvalid, match="cannot be enabled"):
            host.enable("broken")

    def test_a_missing_plugin_says_what_is_installed(self, sandbox_root: Path) -> None:
        sandbox_root.mkdir(parents=True)
        host = PluginHost(Database(":memory:"), sandbox_root)
        host.discover()
        with pytest.raises(mf.ManifestInvalid, match="no plugin called"):
            host.get("nope")


def test_the_reference_plugin_is_valid() -> None:
    """The documented example must actually work, or the docs are a trap."""
    if not (REFERENCE / "word-count").exists():
        pytest.skip("reference plugin not present")
    parsed = mf.load(REFERENCE / "word-count")
    assert parsed.name == "word-count"
    assert parsed.scopes == [], "the reference plugin should need no permissions"
    assert parsed.max_risk is Risk.SAFE
    assert (REFERENCE / "word-count" / f"{parsed.entry}.py").exists()


def test_python_can_be_found_for_plugins() -> None:
    assert Path(sys.executable).exists()


# ── the API ──────────────────────────────────────────────────────────────
class TestPluginApi:
    async def test_listing_says_what_isolation_actually_is(self, client) -> None:
        """The interface must not imply a sandbox that does not exist."""
        body = (await client.get("/plugins")).json()
        assert "no inherited credentials" in body["isolation"]
        assert "not what it can do on its own" in body["isolation"]

    async def test_listing_an_empty_directory_is_not_an_error(self, client) -> None:
        body = (await client.get("/plugins")).json()
        assert body["plugins"] == []
        assert body["directory"]

    async def test_enabling_something_that_is_not_installed_is_refused(self, client) -> None:
        response = await client.post("/plugins/not-installed/enable")
        assert response.status_code == 422
        assert "no plugin called" in response.json()["detail"]

    async def test_plugin_endpoints_need_a_token(self, app) -> None:
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
            assert (await anon.get("/plugins")).status_code == 401
            assert (await anon.post("/plugins/x/enable")).status_code == 401


class TestPluginsInTheRealApp:
    async def test_no_plugin_tools_are_registered_by_default(self, ctx) -> None:
        """Off by default means the registry has none of them on a fresh install."""
        assert [n for n in ctx.registry.names() if n.startswith("plugin.")] == []

    async def test_the_plugin_directory_is_under_the_data_folder(self, ctx) -> None:
        from jarvis.config import paths

        assert ctx.plugins.directory == paths.plugins_dir()

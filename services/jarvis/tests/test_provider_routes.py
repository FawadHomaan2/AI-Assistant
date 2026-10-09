"""Configuring AI providers from the running app.

Before these endpoints the cloud models were unreachable from an installed
copy: `config.toml` ships them commented out, so a fresh install listed
`dev_echo` alone, and the adapters' "add a key in Settings" named a control
that did not exist.

What is pinned here is mostly about the key: that it reaches the OS credential
store and nothing else, that it is never echoed back or written to the audit
log, that whitespace from a paste does not survive, and that a provider already
built is discarded so the key takes effect without a restart.
"""

from __future__ import annotations

import pytest

from jarvis.config import paths, secrets


@pytest.fixture
def store(monkeypatch):
    """An in-memory stand-in for the OS credential store.

    Headless test machines have no keyring backend at all, which is a case worth
    testing on its own (see `TestWithoutACredentialStore`) but makes every
    success path unreachable.
    """
    kept: dict[str, str] = {}

    monkeypatch.setattr(secrets, "set_secret", lambda name, value: kept.__setitem__(name, value))
    monkeypatch.setattr(secrets, "get", lambda name: kept.get(name))
    monkeypatch.setattr(secrets, "has", lambda name: name in kept)
    monkeypatch.setattr(secrets, "delete", lambda name: kept.pop(name, None) is not None)
    monkeypatch.setattr(
        secrets,
        "status",
        lambda: secrets.StoreStatus(True, "InMemory", "Test double."),
    )
    return kept


def _audit_text(ctx) -> str:
    """Every audit row flattened, for asserting a secret is not among them."""
    return repr(ctx.audit.recent(200))


def _config_text() -> str:
    """config.toml, or "" when it does not exist yet.

    A fresh install has no config file: it is written the first time a setting
    is saved. So "nothing was written" means either an unchanged file or still
    no file, and a reader that raises on the second cannot tell the difference.
    """
    path = paths.config_file()
    return path.read_text("utf-8") if path.exists() else ""


class TestPresets:
    async def test_lists_the_services_on_offer(self, client):
        response = await client.get("/providers/presets")
        assert response.status_code == 200
        presets = response.json()["presets"]

        by_id = {p["id"]: p for p in presets}
        # Named rather than counted, so adding a preset without a test is caught.
        assert set(by_id) == {
            "jarvis_chatgpt",
            "jarvis_claude",
            "jarvis_gemini",
            "anthropic",
            "openai",
            "gemini",
            "openrouter",
            "groq",
            "ollama",
            "local",
        }

        assert by_id["anthropic"]["kind"] == "anthropic"
        assert by_id["gemini"]["kind"] == "google"
        assert by_id["anthropic"]["needs_key"] is True
        # The local ones must not ask for a key that does not exist.
        assert by_id["ollama"]["needs_key"] is False
        assert by_id["ollama"]["is_cloud"] is False
        assert by_id["local"]["is_cloud"] is False

    async def test_the_keyless_relay_presets_need_no_key_but_are_still_cloud(self, client):
        """No key is not the same as private, and the panel must not imply it is.

        The relay presets are the only cloud entries with no credential, so
        they are the ones where a missing `is_cloud` would quietly exempt a
        conversation from the `allow_cloud` gate.
        """
        presets = (await client.get("/providers/presets")).json()["presets"]
        relays = [p for p in presets if p["kind"] == "jarvis_cloud"]
        assert len(relays) == 3

        for preset in relays:
            assert preset["needs_key"] is False, preset["id"]
            assert preset["credential"] == "", preset["id"]
            assert preset["is_cloud"] is True, preset["id"]
            # Nothing here should read as private, so the note has to say where
            # the conversation goes rather than only that it is free.
            assert "relay" in preset["note"].lower(), preset["id"]
            assert "sees what you send" in preset["note"], preset["id"]

    async def test_every_cloud_preset_says_where_to_get_a_key(self, client):
        """The point setup otherwise stalls at, for someone without a key."""
        presets = (await client.get("/providers/presets")).json()["presets"]
        for preset in presets:
            if preset["needs_key"]:
                assert preset["key_url"].startswith("https://"), preset["id"]
                assert preset["credential"], preset["id"]


class TestAddingAProvider:
    async def test_adds_one_and_writes_it_to_the_config(self, client, ctx):
        response = await client.post(
            "/providers",
            json={"name": "gemini", "kind": "google", "model": "gemini-2.0-flash"},
        )
        assert response.status_code == 200
        assert "gemini" in {p["name"] for p in response.json()["providers"]}

        # Persisted, not just held in memory: a provider that disappears on
        # restart is worse than one that was never added.
        assert "[ai.providers.gemini]" in paths.config_file().read_text("utf-8")
        assert ctx.settings.ai.providers["gemini"].kind == "google"

    async def test_stores_a_key_in_the_same_call(self, client, ctx, store):
        response = await client.post(
            "/providers",
            json={
                "name": "anthropic",
                "kind": "anthropic",
                "credential": "jarvis/anthropic",
                "key": "sk-ant-secret-value",
            },
        )
        assert response.status_code == 200
        assert store["jarvis/anthropic"] == "sk-ant-secret-value"

        # Not in the response, not in the config file, not in the audit log.
        assert "sk-ant-secret-value" not in response.text
        assert "sk-ant-secret-value" not in _config_text()
        assert "sk-ant-secret-value" not in _audit_text(ctx)

    async def test_a_key_with_no_credential_name_is_refused(self, client, store):
        """There would be nowhere to put it, and silently dropping it is worse."""
        response = await client.post(
            "/providers",
            json={"name": "anthropic", "kind": "anthropic", "key": "sk-ant-x"},
        )
        assert response.status_code == 422
        assert store == {}

    async def test_an_unusable_name_is_refused_and_changes_nothing(self, client):
        before = _config_text()
        response = await client.post("/providers", json={"name": "has space", "kind": "ollama"})
        assert response.status_code >= 400
        assert _config_text() == before

    async def test_a_missing_kind_is_refused(self, client):
        response = await client.post("/providers", json={"name": "mystery"})
        assert response.status_code == 422

    async def test_an_unknown_kind_is_refused(self, client):
        response = await client.post("/providers", json={"name": "x", "kind": "not_real"})
        assert response.status_code >= 400

    async def test_a_key_pasted_into_the_credential_field_is_refused(self, client, store):
        """`credential` names a keychain entry. config.toml is plaintext."""
        response = await client.post(
            "/providers",
            json={"name": "anthropic", "kind": "anthropic", "credential": "sk-ant-oops"},
        )
        assert response.status_code >= 400
        assert "sk-ant-oops" not in _config_text()


class TestTheStoredKey:
    async def test_set_then_report_configured(self, client, store):
        await client.post(
            "/providers",
            json={"name": "gemini", "kind": "google", "credential": "jarvis/gemini"},
        )
        listed = {p["name"]: p for p in (await client.get("/providers")).json()}
        assert listed["gemini"]["configured"] is False

        response = await client.put("/providers/gemini/credential", json={"key": "abc123"})
        assert response.status_code == 200
        assert store["jarvis/gemini"] == "abc123"

        listed = {p["name"]: p for p in (await client.get("/providers")).json()}
        assert listed["gemini"]["configured"] is True

    async def test_whitespace_from_a_paste_is_stripped(self, client, store):
        """A trailing newline in an API key becomes an invalid HTTP header.

        The client rejects it with a message that says nothing about the key,
        and a key copied out of a browser usually carries one.
        """
        await client.post(
            "/providers",
            json={"name": "gemini", "kind": "google", "credential": "jarvis/gemini"},
        )
        await client.put("/providers/gemini/credential", json={"key": "  abc123\n"})
        assert store["jarvis/gemini"] == "abc123"

    async def test_an_empty_key_is_refused(self, client, store):
        await client.post(
            "/providers",
            json={"name": "gemini", "kind": "google", "credential": "jarvis/gemini"},
        )
        for empty in ("", "   ", "\n"):
            response = await client.put("/providers/gemini/credential", json={"key": empty})
            assert response.status_code == 422, empty
        assert store == {}

    async def test_a_provider_that_is_not_configured_is_a_404(self, client, store):
        response = await client.put("/providers/nope/credential", json={"key": "x"})
        assert response.status_code == 404

    async def test_a_provider_that_needs_no_key_says_so(self, client, store):
        await client.post("/providers", json={"name": "ollama", "kind": "ollama"})
        response = await client.put("/providers/ollama/credential", json={"key": "x"})
        assert response.status_code == 422
        assert "no credential name" in response.json()["detail"]

    async def test_storing_a_key_discards_the_provider_already_built(self, client, ctx, store):
        """The reason a key works without a restart.

        A provider bakes its key into an HTTP client's headers when it is
        constructed, and the gateway caches it. Without dropping that cache, a
        key stored now would not be used until the next launch.
        """
        await client.post(
            "/providers",
            json={"name": "gemini", "kind": "google", "credential": "jarvis/gemini"},
        )
        before = ctx.gateway.provider_by_name("gemini")

        await client.put("/providers/gemini/credential", json={"key": "abc123"})

        assert ctx.gateway.provider_by_name("gemini") is not before

    async def test_clearing_a_key(self, client, store):
        await client.post(
            "/providers",
            json={
                "name": "gemini",
                "kind": "google",
                "credential": "jarvis/gemini",
                "key": "abc123",
            },
        )
        assert store["jarvis/gemini"] == "abc123"

        response = await client.delete("/providers/gemini/credential")
        assert response.status_code == 200
        assert response.json()["removed"] is True
        assert store == {}

        listed = {p["name"]: p for p in (await client.get("/providers")).json()}
        assert listed["gemini"]["configured"] is False


class TestWithoutACredentialStore:
    """The headless case, and the one the frozen installer hit.

    `keyring` is an optional extra, and `jarvis.config.secrets` refuses to fall
    back to a file rather than turning a secure store into a plaintext one. What
    must not happen is a key appearing to be saved when it was not.
    """

    async def test_storing_a_key_fails_loudly(self, client):
        await client.post(
            "/providers",
            json={"name": "gemini", "kind": "google", "credential": "jarvis/gemini"},
        )
        response = await client.put("/providers/gemini/credential", json={"key": "abc123"})
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert "keyring" in detail.lower() or "credential" in detail.lower()
        # Says what to do, rather than only that something failed.
        assert "install" in detail.lower() or "extra" in detail.lower()

    async def test_the_provider_is_still_added(self, client, ctx):
        """Adding it and keying it are separate steps, so one can fail alone."""
        response = await client.post(
            "/providers",
            json={"name": "gemini", "kind": "google", "credential": "jarvis/gemini"},
        )
        assert response.status_code == 200
        assert "gemini" in ctx.settings.ai.providers


class TestRemovingAProvider:
    async def test_removes_the_provider_and_its_key(self, client, ctx, store):
        await client.post(
            "/providers",
            json={
                "name": "gemini",
                "kind": "google",
                "credential": "jarvis/gemini",
                "key": "abc123",
            },
        )
        response = await client.delete("/providers/gemini")
        assert response.status_code == 200
        assert response.json()["key_removed"] is True
        assert store == {}
        assert "gemini" not in ctx.settings.ai.providers

    async def test_keeping_the_key_is_possible(self, client, store):
        await client.post(
            "/providers",
            json={
                "name": "gemini",
                "kind": "google",
                "credential": "jarvis/gemini",
                "key": "abc123",
            },
        )
        response = await client.delete("/providers/gemini?forget_key=false")
        assert response.status_code == 200
        assert response.json()["key_removed"] is False
        assert store["jarvis/gemini"] == "abc123"

    async def test_removing_the_one_in_use_moves_the_default(self, client, ctx, store):
        await client.post("/providers", json={"name": "ollama", "kind": "ollama"})
        await client.post("/ai", json={"default": "ollama"})
        assert ctx.settings.ai.default == "ollama"

        response = await client.delete("/providers/ollama")
        assert response.status_code == 200
        # Otherwise every later request fails with "not configured" — a dead end
        # reached by deleting something else.
        assert response.json()["default"] != "ollama"
        assert ctx.settings.ai.default in ctx.settings.ai.providers

    async def test_removing_one_that_is_not_there_is_a_404(self, client):
        assert (await client.delete("/providers/never-existed")).status_code == 404


class TestAiSettings:
    async def test_changes_the_provider_in_use(self, client, ctx):
        await client.post("/providers", json={"name": "ollama", "kind": "ollama"})
        response = await client.post("/ai", json={"default": "ollama"})
        assert response.status_code == 200
        assert response.json()["default"] == "ollama"
        assert ctx.settings.ai.default == "ollama"
        assert 'default = "ollama"' in paths.config_file().read_text("utf-8")

    async def test_defaulting_to_something_unconfigured_is_refused(self, client, ctx):
        response = await client.post("/ai", json={"default": "typo"})
        assert response.status_code >= 400
        assert ctx.settings.ai.default == "dev_echo"

    async def test_cloud_access_is_its_own_decision(self, client, ctx):
        """Storing a key must not start sending conversations off the machine."""
        assert ctx.settings.ai.allow_cloud is False
        response = await client.post("/ai", json={"allow_cloud": True})
        assert response.status_code == 200
        assert response.json()["allow_cloud"] is True
        assert ctx.settings.ai.allow_cloud is True
        # The content flag is separate again, and was not asked for.
        assert ctx.settings.ai.allow_cloud_content is False

    async def test_adding_a_cloud_provider_does_not_allow_cloud(self, client, ctx, store):
        await client.post(
            "/providers",
            json={
                "name": "anthropic",
                "kind": "anthropic",
                "credential": "jarvis/anthropic",
                "key": "sk-ant-x",
            },
        )
        assert ctx.settings.ai.allow_cloud is False

    async def test_the_change_reaches_the_gateway(self, client, ctx):
        """Not only `ctx.settings`: the gateway reads the flags on every call."""
        await client.post("/ai", json={"allow_cloud": True, "allow_cloud_content": True})
        assert ctx.gateway.settings.ai.allow_cloud is True
        assert ctx.gateway.settings.ai.allow_cloud_content is True

    async def test_an_empty_body_changes_nothing(self, client, ctx):
        response = await client.post("/ai", json={})
        assert response.status_code == 200
        assert ctx.settings.ai.default == "dev_echo"
        assert ctx.settings.ai.allow_cloud is False


class TestAudit:
    async def test_every_change_is_recorded(self, client, ctx, store):
        await client.post(
            "/providers",
            json={
                "name": "gemini",
                "kind": "google",
                "credential": "jarvis/gemini",
                "key": "abc123",
            },
        )
        await client.put("/providers/gemini/credential", json={"key": "def456"})
        await client.post("/ai", json={"allow_cloud": True})
        await client.delete("/providers/gemini")

        actions = [row["action"] for row in ctx.audit.recent(50)]
        assert "provider.add" in actions
        assert "provider.credential.set" in actions
        assert "ai.settings" in actions
        assert "provider.remove" in actions

        # The log proves what happened without holding what was said.
        text = _audit_text(ctx)
        assert "abc123" not in text
        assert "def456" not in text

    async def test_the_chain_still_verifies(self, client, ctx, store):
        await client.post("/providers", json={"name": "ollama", "kind": "ollama"})
        await client.post("/ai", json={"default": "ollama"})
        intact, broken_at = ctx.audit.verify()
        assert intact, f"audit chain broken at {broken_at}"

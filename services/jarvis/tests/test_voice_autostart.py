"""Listening from launch, and refusing to.

`[voice] enabled` was dead configuration for two releases: read from
config.toml, passed into the pipeline, reported over the API, and never acted
on. Turning it on changed nothing, so the wake word had to be started by hand
every launch, which is not what a wake word is for.

Acting on it means a microphone can open without anyone pressing anything, so
what is pinned here is mostly the refusals: the setting alone is not enough,
and every condition that stops it is reported rather than raised.
"""

from __future__ import annotations

import pytest

from jarvis.app import start_listening_if_asked
from jarvis.config import paths
from jarvis.governance.scopes import Scope


@pytest.fixture
def started(ctx, monkeypatch):
    """Record whether the microphone was opened, without opening one."""
    calls: list[bool] = []

    async def fake_start() -> None:
        calls.append(True)

    monkeypatch.setattr(ctx.capture, "start", fake_start)
    return calls


@pytest.fixture
def ready(ctx, monkeypatch):
    """A machine where listening would in fact work."""
    monkeypatch.setattr(ctx.voice, "unavailable_reason", lambda: "")
    monkeypatch.setattr(ctx.capture, "availability", lambda: (True, "Microphone ready."))
    return ctx


def _grant_microphone(ctx) -> None:
    ctx.policy.grants.grant(Scope.MIC_LISTEN, "")


class TestItStaysClosedUnlessAsked:
    async def test_the_setting_off_means_nothing_happens(self, ctx, ready, started):
        ctx.settings.voice.enabled = False
        _grant_microphone(ctx)
        await start_listening_if_asked(ctx)
        assert started == []

    async def test_the_setting_alone_is_not_enough(self, ctx, ready, started):
        """Without `mic.listen` the setting cannot open a microphone.

        Two independent gates, so one of them being set by mistake — or by
        something editing config.toml — does not start recording.
        """
        ctx.settings.voice.enabled = True
        assert Scope.MIC_LISTEN not in ctx.policy.grants.granted
        await start_listening_if_asked(ctx)
        assert started == []

    async def test_an_engaged_emergency_stop_wins(self, ctx, ready, started):
        ctx.settings.voice.enabled = True
        _grant_microphone(ctx)
        ctx.estop.engage()
        await start_listening_if_asked(ctx)
        assert started == []

    async def test_a_missing_model_stops_it_without_failing(self, ctx, started, monkeypatch):
        ctx.settings.voice.enabled = True
        _grant_microphone(ctx)
        monkeypatch.setattr(
            ctx.voice, "unavailable_reason", lambda: "Voice is not ready: wake-word missing."
        )
        monkeypatch.setattr(ctx.capture, "availability", lambda: (True, "ok"))
        # Returns rather than raising: refusing to launch over a missing voice
        # model would be a far worse bargain than not listening.
        await start_listening_if_asked(ctx)
        assert started == []

    async def test_no_microphone_stops_it_without_failing(self, ctx, started, monkeypatch):
        ctx.settings.voice.enabled = True
        _grant_microphone(ctx)
        monkeypatch.setattr(ctx.voice, "unavailable_reason", lambda: "")
        monkeypatch.setattr(
            ctx.capture, "availability", lambda: (False, "No input device was found.")
        )
        await start_listening_if_asked(ctx)
        assert started == []

    async def test_a_failure_to_open_is_caught(self, ctx, ready, monkeypatch):
        from jarvis.voice.capture import CaptureError

        ctx.settings.voice.enabled = True
        _grant_microphone(ctx)

        async def explode() -> None:
            raise CaptureError("The microphone was busy.")

        monkeypatch.setattr(ctx.capture, "start", explode)
        # The core must still come up.
        await start_listening_if_asked(ctx)


class TestItOpensWhenEverythingIsInPlace:
    async def test_listens_at_launch(self, ctx, ready, started):
        ctx.settings.voice.enabled = True
        _grant_microphone(ctx)
        await start_listening_if_asked(ctx)
        assert started == [True], "the wake word should be live without a button press"

    async def test_the_audit_log_records_who_opened_it(self, ctx, ready, started):
        ctx.settings.voice.enabled = True
        _grant_microphone(ctx)
        await start_listening_if_asked(ctx)

        rows = [row for row in ctx.audit.recent(20) if row["action"] == "voice.listen"]
        assert rows, "opening the microphone must be in the audit log"
        # `actor` is what distinguishes this from someone pressing the button;
        # "was it listening at 3pm" needs an answer either way.
        assert rows[-1]["actor"] == "system"
        assert rows[-1]["risk"] == "elevated"
        intact, broken_at = ctx.audit.verify()
        assert intact, f"audit chain broken at {broken_at}"


class TestTheSettingsRoute:
    async def test_turning_it_on_persists_and_warns_about_the_permission(self, client, ctx):
        response = await client.post("/voice/settings", json={"enabled": True})
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        # The setting is on and the permission is not: say so rather than let
        # someone discover nothing happens at the next launch.
        assert body["needsMicPermission"] is True

        assert ctx.settings.voice.enabled is True
        assert "enabled = true" in paths.config_file().read_text("utf-8")

    async def test_no_warning_once_the_permission_is_granted(self, client, ctx):
        _grant_microphone(ctx)
        body = (await client.post("/voice/settings", json={"enabled": True})).json()
        assert body["enabled"] is True
        assert body["needsMicPermission"] is False

    async def test_turning_it_off_again(self, client, ctx):
        await client.post("/voice/settings", json={"enabled": True})
        body = (await client.post("/voice/settings", json={"enabled": False})).json()
        assert body["enabled"] is False
        assert ctx.settings.voice.enabled is False

    async def test_an_empty_body_changes_nothing(self, client, ctx):
        before = ctx.settings.voice.enabled
        response = await client.post("/voice/settings", json={})
        assert response.status_code == 200
        assert ctx.settings.voice.enabled is before

    async def test_an_unusable_wake_word_is_refused(self, client, ctx):
        response = await client.post("/voice/settings", json={"wake_word": "has space"})
        assert response.status_code >= 400
        assert ctx.settings.voice.wake_word == "hey_jarvis"

    async def test_the_change_is_audited(self, client, ctx):
        await client.post("/voice/settings", json={"enabled": True})
        actions = [row["action"] for row in ctx.audit.recent(20)]
        assert "voice.settings" in actions

    async def test_status_reports_the_setting(self, client):
        await client.post("/voice/settings", json={"enabled": True})
        status = (await client.get("/voice/status")).json()
        assert status["settings"]["enabled"] is True

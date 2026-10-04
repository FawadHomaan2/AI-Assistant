"""Voice control over the API.

The point of these is the refusals. A permanently open microphone is the most
invasive capability in this program, so "it opened when it should not have" is a
far worse bug than "it failed to open", and most of what follows checks that the
gates hold rather than that the happy path works.
"""

from __future__ import annotations

import pytest

from jarvis.governance.scopes import Scope
from jarvis.voice.capture import FrameListSource, UnavailableSource


@pytest.fixture
def mic_granted(ctx):  # type: ignore[no-untyped-def]
    ctx.policy.grants.grant(Scope.MIC_LISTEN, ttl_minutes=60)
    return ctx


class TestStatus:
    async def test_status_separates_the_microphone_from_the_models(self, client) -> None:  # type: ignore[no-untyped-def]
        """Two different problems with two different fixes.

        A missing download is something the interface can offer to fix; no audio
        device is not. Reporting both as "voice is not ready" sends people to the
        wrong place.
        """
        body = (await client.get("/voice/status")).json()
        assert "microphone" in body
        mic = body["microphone"]
        assert set(mic) >= {"available", "detail", "listening", "framesSeen", "framesDropped"}
        assert mic["detail"], "an unavailable microphone must say why"
        assert mic["listening"] is False

    async def test_status_reports_whether_the_mic_scope_is_granted(self, client, ctx) -> None:  # type: ignore[no-untyped-def]
        assert (await client.get("/voice/status")).json()["micScopeGranted"] is False
        ctx.policy.grants.grant(Scope.MIC_LISTEN, ttl_minutes=60)
        assert (await client.get("/voice/status")).json()["micScopeGranted"] is True


class TestListenIsGated:
    async def test_listening_is_refused_without_the_mic_scope(self, client, ctx) -> None:  # type: ignore[no-untyped-def]
        """Deny by default. Configuring voice does not imply consent to listen."""
        assert Scope.MIC_LISTEN not in ctx.policy.grants.granted
        response = await client.post("/voice/listen")
        assert response.status_code == 403
        assert "mic.listen" in response.json()["detail"]
        assert not ctx.capture.running

    async def test_listening_is_refused_while_the_emergency_stop_is_engaged(
        self,
        client,
        mic_granted,  # type: ignore[no-untyped-def]
    ) -> None:
        mic_granted.estop.engage("test")
        response = await client.post("/voice/listen")
        assert response.status_code == 409
        assert not mic_granted.capture.running

    async def test_an_absent_microphone_is_a_503_with_the_reason(
        self,
        client,
        mic_granted,  # type: ignore[no-untyped-def]
    ) -> None:
        mic_granted.capture.source = UnavailableSource("no microphone is connected")
        response = await client.post("/voice/listen")
        assert response.status_code == 503
        assert "no microphone is connected" in response.json()["detail"]

    async def test_granting_the_scope_lets_listening_start(
        self,
        client,
        mic_granted,  # type: ignore[no-untyped-def]
    ) -> None:
        # A source with no frames: enough to prove the gate opened and the
        # pipeline moved, without needing audio hardware.
        mic_granted.capture.source = FrameListSource([])
        mic_granted.voice.settings.wake_word_enabled = False
        response = await client.post("/voice/listen")
        assert response.status_code == 200, response.text
        assert response.json()["listening"] is True
        await client.post("/voice/stop")


class TestStopAlwaysWorks:
    async def test_stopping_when_not_listening_is_not_an_error(self, client) -> None:  # type: ignore[no-untyped-def]
        response = await client.post("/voice/stop")
        assert response.status_code == 200
        assert response.json()["listening"] is False

    async def test_the_emergency_stop_closes_the_microphone(
        self,
        client,
        mic_granted,  # type: ignore[no-untyped-def]
    ) -> None:
        """An emergency stop that leaves the microphone open has not stopped
        the thing most people press it for."""
        mic_granted.capture.source = FrameListSource([])
        mic_granted.voice.settings.wake_word_enabled = False
        await client.post("/voice/listen")

        body = (await client.post("/emergency-stop")).json()
        assert body["microphoneClosed"] is True
        assert not mic_granted.capture.running


class TestPreparingTheSpeechModel:
    async def test_prepare_reports_status_without_the_voice_extra(self, client) -> None:  # type: ignore[no-untyped-def]
        """Its own endpoint because of a deadlock.

        The pipeline refused to listen while the speech model was missing, and
        the model only downloaded on the first transcription — which needed
        listening to have started. Without this route there is no path from a
        fresh install to a working microphone.
        """
        response = await client.post("/voice/stt/prepare")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "speech-to-text"
        # Without faster-whisper installed it says so rather than pretending.
        assert body["detail"]
        assert "ready" in body

    async def test_prepare_does_not_need_the_mic_scope(self, client) -> None:  # type: ignore[no-untyped-def]
        """Downloading a model is not listening, so it is not gated on the
        microphone permission. Conflating the two would mean granting a
        standing microphone permission just to fetch a file."""
        assert (await client.post("/voice/stt/prepare")).status_code == 200

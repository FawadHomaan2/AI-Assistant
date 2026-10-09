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


@pytest.fixture
def stub_speech(ctx, monkeypatch):  # type: ignore[no-untyped-def]
    """Never let a test actually fetch the speech model.

    `stt.prepare()` downloads ~74 MB through huggingface_hub. It is a no-op
    where faster-whisper is not installed, which is how CI's main job runs —
    so the two tests below passed there while quietly pulling 74 MB off the
    network for anyone with the voice extra installed, twice, into a tmp
    directory thrown away immediately after.

    Recorded rather than removed: some of these tests are about whether the
    route calls it at all.
    """
    from jarvis.voice.types import ComponentStatus

    calls: list[bool] = []

    def prepare() -> ComponentStatus:
        calls.append(True)
        return ComponentStatus(
            name="speech-to-text", available=True, detail="Stubbed for the test."
        )

    monkeypatch.setattr(ctx.voice.stt, "prepare", prepare)
    return calls


class TestPreparingTheSpeechModel:
    async def test_prepare_reports_the_component_and_the_pipeline(  # type: ignore[no-untyped-def]
        self, client, stub_speech
    ) -> None:
        """Its own endpoint because of a deadlock.

        The pipeline refused to listen while the speech model was missing, and
        the model only downloaded on the first transcription — which needed
        listening to have started. Without this route there is no path from a
        fresh install to a working microphone.

        What is checked is the response shape, which the panel reads: the
        component's own status plus whether the pipeline as a whole is ready.
        This used to assert a `detail` that only said anything when
        faster-whisper was absent, so it tested the environment rather than
        the route.
        """
        response = await client.post("/voice/stt/prepare")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "speech-to-text"
        assert body["detail"], "the component must say where it stands either way"
        # The pipeline's verdict, not just this component's: two of three in
        # place is still a wake word that cannot start.
        assert "ready" in body
        assert "reason" in body
        assert stub_speech == [True], "the route has to actually prepare the model"

    async def test_prepare_does_not_need_the_mic_scope(self, client, stub_speech) -> None:  # type: ignore[no-untyped-def]
        """Downloading a model is not listening, so it is not gated on the
        microphone permission. Conflating the two would mean granting a
        standing microphone permission just to fetch a file."""
        assert (await client.post("/voice/stt/prepare")).status_code == 200


class TestFetchingEveryVoiceModel:
    """One request for all three, because two routes were needed and one of
    them was never called.

    The wake word and the Piper voice come from the model list; the speech
    model has no catalogue URL, so it has no Download button there and only
    `POST /voice/stt/prepare` fetches it. Nothing in the interface called that,
    and readiness needs all three components — so no sequence of clicks in the
    shipped app ended with a working wake word. This route is what the Voice
    panel calls instead.
    """

    @pytest.fixture
    def no_network(self, monkeypatch, stub_speech):  # type: ignore[no-untyped-def]
        """Every catalogue fetch fails, as it would offline."""
        from jarvis.config import models as models_module

        def refuse(key: str, opener: object = None) -> object:
            raise models_module.ModelError(f"Could not reach the server for {key}.")

        monkeypatch.setattr(models_module, "fetch", refuse)
        return monkeypatch

    @pytest.fixture
    def downloads_work(self, monkeypatch, tmp_path, stub_speech):  # type: ignore[no-untyped-def]
        """Every catalogue fetch succeeds, without a network or 141 MB of disk."""
        from jarvis.config import models as models_module

        asked: list[str] = []

        def pretend(key: str, opener: object = None) -> object:
            asked.append(key)
            return tmp_path / key

        monkeypatch.setattr(models_module, "fetch", pretend)
        return asked

    async def test_it_asks_for_the_wake_word_and_the_voice(self, client, downloads_work) -> None:  # type: ignore[no-untyped-def]
        response = await client.post("/voice/models/fetch")
        assert response.status_code == 200
        # `models.fetch` resolves each one's `requires` itself, so these two
        # keys cover the three openWakeWord graphs and Piper's JSON config.
        assert downloads_work == ["openwakeword-hey-jarvis", "piper-en-us"]

    async def test_the_keys_it_asks_for_really_exist(self) -> None:
        """A typo here would be a download that always fails, named nowhere."""
        from jarvis.config import models as models_module
        from jarvis.transport.routes import VOICE_MODEL_KEYS

        for key in VOICE_MODEL_KEYS:
            assert key in models_module.BY_KEY, key

    async def test_it_also_prepares_the_speech_model(  # type: ignore[no-untyped-def]
        self, client, downloads_work, stub_speech
    ) -> None:
        """The half the interface never reached.

        Without this the other two downloads are wasted: readiness needs all
        three, so the wake word stays unavailable with two of three in place.
        """
        response = await client.post("/voice/models/fetch")
        assert response.status_code == 200
        assert stub_speech == [True], "the speech model must be requested too"

    async def test_a_failure_is_reported_not_raised(self, client, no_network) -> None:  # type: ignore[no-untyped-def]
        """141 MB over three sources is where one part fails and the rest hold.

        A 500 would throw away whatever did arrive and leave the panel with
        nothing to say beyond that something went wrong.
        """
        response = await client.post("/voice/models/fetch")
        assert response.status_code == 200
        body = response.json()
        assert body["failed"], "a failed download has to appear in the response"
        # The reason, named per model: "the download failed" after several
        # minutes is not something anyone can act on.
        assert all(entry["model"] and entry["error"] for entry in body["failed"])
        assert body["ready"] is False
        assert body["reason"]

    async def test_it_reports_what_is_still_missing(self, client, no_network) -> None:
        body = (await client.post("/voice/models/fetch")).json()
        # The panel shows this, so it has to name components rather than say
        # "not ready".
        assert "not ready" in body["reason"].lower()

    async def test_the_download_is_audited(self, client, ctx, downloads_work) -> None:  # type: ignore[no-untyped-def]
        await client.post("/voice/models/fetch")
        actions = [row["action"] for row in ctx.audit.recent(20)]
        assert "voice.models.fetch" in actions
        intact, broken_at = ctx.audit.verify()
        assert intact, f"audit chain broken at {broken_at}"

    async def test_it_does_not_need_the_microphone_permission(self, client, downloads_work) -> None:  # type: ignore[no-untyped-def]
        """Fetching a file is not listening, same as the route above."""
        assert (await client.post("/voice/models/fetch")).status_code == 200

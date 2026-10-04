"""The streaming channel the UI actually uses."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tests.conftest import TOKEN


@pytest.fixture
def ws_client(app):
    with TestClient(app) as client:
        yield client


def test_requires_a_token(ws_client) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc, ws_client.websocket_connect("/ws"):
        pass
    assert exc.value.code == 4401


def test_rejects_cross_origin(ws_client) -> None:
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as exc,
        ws_client.websocket_connect(
            f"/ws?token={TOKEN}", headers={"origin": "https://evil.example"}
        ),
    ):
        pass
    assert exc.value.code == 4403


def test_token_via_query_parameter(ws_client) -> None:
    """Browsers cannot set headers on a WebSocket handshake."""
    with ws_client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_chat_streams_events_in_order(ws_client) -> None:
    with ws_client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"type": "chat", "message": "hello there"})
        kinds, text = [], ""
        while True:
            event = ws.receive_json()
            kinds.append(event["type"])
            if event["type"] == "delta":
                text += event["text"]
            if event["type"] == "turn.end":
                break

    assert kinds[0] == "turn.start"
    assert kinds[1] == "route"
    assert kinds.count("delta") > 5, "must stream incrementally"
    assert "not a language model" in text


def test_a_computer_task_is_never_answered_by_the_model(ws_client) -> None:
    """A computer task goes to the tool layer, whatever the outcome there.

    This used to send "Open Chrome" and assert a notice came back, which was
    right in Phase 2 when computer tasks were intercepted. Phase 4 made
    launching real, so on any machine that actually has Chrome — every GitHub
    runner, for one — Jarvis correctly launched it and the test failed. It also
    meant the suite launched a browser as a side effect.

    The invariant worth holding is the one that was always the point: the model
    never gets to claim it did something. So the request names an application
    that cannot exist anywhere, and the assertion is about the path taken.
    """
    with ws_client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"type": "chat", "message": "Open flurbleglorp"})
        kinds = []
        while True:
            event = ws.receive_json()
            kinds.append(event["type"])
            if event["type"] == "turn.end":
                break
    assert "delta" not in kinds, "a computer task must never be answered by the model"
    assert "plan" in kinds, "it must reach the tool layer"
    # Nothing was launched, and Jarvis said so rather than claiming success.
    assert "notice" in kinds or "error" in kinds


def test_session_is_reused_across_messages(ws_client) -> None:
    with ws_client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"type": "chat", "message": "first"})
        session_id = None
        while True:
            event = ws.receive_json()
            session_id = session_id or event.get("session_id")
            if event["type"] == "turn.end":
                break

        ws.send_json({"type": "chat", "message": "second", "session_id": session_id})
        while True:
            event = ws.receive_json()
            assert event["session_id"] == session_id
            if event["type"] == "turn.end":
                break


def test_malformed_json_is_reported(ws_client) -> None:
    with ws_client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_text("{not json")
        event = ws.receive_json()
        assert event["type"] == "error"
        assert event["code"] == "jarvis.bad_request"


def test_unknown_message_type_is_reported(ws_client) -> None:
    with ws_client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"type": "nonsense"})
        assert ws.receive_json()["code"] == "jarvis.bad_request"

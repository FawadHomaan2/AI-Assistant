"""HTTP surface, including the loopback auth controls."""

from __future__ import annotations

import httpx
import pytest


class TestAuth:
    async def test_health_is_public(self, app) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.get("/health")).status_code == 200

    @pytest.mark.parametrize("path", ["/providers", "/sessions", "/audit"])
    async def test_everything_else_needs_a_token(self, app, path: str) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(path)
            assert r.status_code == 401
            assert r.json()["code"] == "jarvis.auth.token"

    async def test_wrong_token_rejected(self, app) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t", headers={"authorization": "Bearer nope"}
        ) as c:
            assert (await c.get("/providers")).status_code == 401

    async def test_cross_origin_refused(self, client) -> None:
        """A web page must not be able to drive the API even with the port."""
        r = await client.get("/providers", headers={"origin": "https://evil.example"})
        assert r.status_code == 403
        assert r.json()["code"] == "jarvis.auth.origin"

    @pytest.mark.parametrize(
        "origin",
        ["http://tauri.localhost", "tauri://localhost", "http://localhost:5183"],
    )
    async def test_shell_origins_allowed(self, client, origin: str) -> None:
        assert (await client.get("/providers", headers={"origin": origin})).status_code == 200

    async def test_token_is_compared_in_constant_time(self) -> None:
        import inspect

        from jarvis.transport import auth

        assert "compare_digest" in inspect.getsource(auth._constant_time_eq)


class TestHealth:
    async def test_reports_real_state(self, client) -> None:
        body = (await client.get("/health")).json()
        assert body["status"] == "ok"
        assert body["schema_version"] >= 1
        assert body["emergency_stop"] is False
        assert body["default_provider"] == "dev_echo"
        assert "available" in body["credential_store"]


class TestSessions:
    async def test_create_list_and_fetch_turns(self, client) -> None:
        session = (await client.post("/sessions")).json()
        assert session["id"].startswith("ses_")

        await client.post("/chat", json={"message": "hello", "session_id": session["id"]})
        turns = (await client.get(f"/sessions/{session['id']}/turns")).json()
        assert [t["role"] for t in turns] == ["user", "assistant"]

        listed = (await client.get("/sessions")).json()
        assert session["id"] in [s["id"] for s in listed]

    async def test_unknown_session_is_404(self, client) -> None:
        assert (await client.get("/sessions/ses_nope/turns")).status_code == 404

    async def test_clear_history_deletes_everything(self, client) -> None:
        for _ in range(3):
            s = (await client.post("/sessions")).json()
            await client.post("/chat", json={"message": "hi", "session_id": s["id"]})
        assert (await client.delete("/sessions")).json()["deleted"] == 3
        assert (await client.get("/sessions")).json() == []


class TestChat:
    async def test_chat_returns_text_and_events(self, client) -> None:
        body = (await client.post("/chat", json={"message": "hello"})).json()
        assert body["text"]
        assert [e["type"] for e in body["events"]][:2] == ["turn.start", "route"]
        assert body["session_id"].startswith("ses_")

    async def test_computer_task_never_reaches_the_model(self, client) -> None:
        """Names an application that cannot exist, so the outcome is the same
        everywhere. Asking for Chrome made this depend on whether the test
        machine had Chrome — and on CI, which does, Jarvis correctly launched
        it and the assertion failed."""
        body = (await client.post("/chat", json={"message": "Open flurbleglorp"})).json()
        types = [e["type"] for e in body["events"]]
        assert "delta" not in types, "a computer task must never be answered by the model"
        assert "plan" in types, "it must reach the tool layer"
        assert body["text"] == ""

    async def test_empty_message_is_rejected_by_validation(self, client) -> None:
        assert (await client.post("/chat", json={"message": ""})).status_code == 422

    async def test_oversized_message_is_rejected(self, client) -> None:
        r = await client.post("/chat", json={"message": "x" * 40_000})
        assert r.status_code == 422


class TestEmergencyStop:
    async def test_engage_blocks_chat_then_clears(self, client) -> None:
        await client.post("/emergency-stop")
        assert (await client.get("/health")).json()["emergency_stop"] is True

        body = (await client.post("/chat", json={"message": "hello"})).json()
        assert body["events"][0]["code"] == "jarvis.emergency_stop"

        await client.delete("/emergency-stop")
        assert (await client.get("/health")).json()["emergency_stop"] is False
        assert (await client.post("/chat", json={"message": "hello"})).json()["text"]


class TestAudit:
    async def test_audit_exposes_chain_integrity(self, client) -> None:
        await client.post("/chat", json={"message": "hello"})
        body = (await client.get("/audit")).json()
        assert body["chain_intact"] is True
        assert body["entries"]
        # Arguments are digested, never stored raw.
        assert all("hello" not in str(e["args_digest"]) for e in body["entries"])


class TestProviders:
    async def test_lists_capabilities(self, client) -> None:
        body = (await client.get("/providers")).json()
        assert body[0]["name"] == "dev_echo"
        assert body[0]["is_cloud"] is False
        assert "not a language model" in body[0]["detail"]

    async def test_health_probe(self, client) -> None:
        body = (await client.get("/providers/health")).json()
        assert body["dev_echo"]["ok"] is True


class TestCors:
    """The webview is a different origin to the core, so CORS headers decide
    whether a legitimate caller ever sees the response body."""

    async def test_allowed_origin_is_echoed_back(self, client) -> None:
        r = await client.get("/providers", headers={"origin": "http://tauri.localhost"})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "http://tauri.localhost"
        assert r.headers["vary"] == "Origin"

    async def test_no_wildcard_is_used(self, client) -> None:
        r = await client.get("/providers", headers={"origin": "http://localhost:5183"})
        assert r.headers["access-control-allow-origin"] != "*"
        assert r.headers["access-control-allow-origin"] == "http://localhost:5183"

    async def test_preflight_succeeds_without_a_token(self, app) -> None:
        """Browsers never attach Authorization to a preflight."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.request(
                "OPTIONS",
                "/chat",
                headers={
                    "origin": "http://tauri.localhost",
                    "access-control-request-method": "POST",
                    "access-control-request-headers": "authorization",
                },
            )
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == "http://tauri.localhost"
        assert "authorization" in r.headers["access-control-allow-headers"]

    async def test_disallowed_origin_gets_no_cors_header(self, client) -> None:
        r = await client.get("/providers", headers={"origin": "https://evil.example"})
        assert r.status_code == 403
        assert "access-control-allow-origin" not in r.headers

    async def test_preflight_from_a_bad_origin_is_refused(self, app) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.request("OPTIONS", "/chat", headers={"origin": "https://evil.example"})
        assert r.status_code == 403

    async def test_401_still_carries_cors_so_the_error_is_readable(self, app) -> None:
        """Without this the UI sees an opaque network failure, not 'bad token'."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/providers", headers={"origin": "http://tauri.localhost"})
        assert r.status_code == 401
        assert r.headers["access-control-allow-origin"] == "http://tauri.localhost"

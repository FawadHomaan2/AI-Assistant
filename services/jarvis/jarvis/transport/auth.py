"""Loopback API authentication.

The core can drive the computer, so "it only listens on localhost" is not enough
on a multi-user machine or one running untrusted local code. Three controls:

  1. Bind 127.0.0.1 only (enforced in settings, not configurable).
  2. A random bearer token minted per launch, handed to the Rust parent over the
     stdout handshake. It is never written to disk and never reused.
  3. Origin checks, so a web page in a browser cannot drive the API through a
     cross-origin request even if it guesses the port.

The webview runs on its own origin (`http://tauri.localhost` on Windows), so
calls into the core ARE cross-origin and the browser enforces CORS. Allowed
origins therefore get `Access-Control-Allow-Origin` echoed back; everything else
gets 403 before reaching a handler. The origin check and the CORS header are two
halves of the same control — rejecting an origin is not enough on its own, since
without the header the browser withholds even a successful response from the
legitimate caller.
"""

from __future__ import annotations

import hmac
import secrets as pysecrets
from urllib.parse import urlparse

from fastapi import Request, WebSocket, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from jarvis.util.logging import get_logger

log = get_logger(__name__)

TOKEN_BYTES = 32

#: Endpoints reachable without a token. Deliberately tiny: liveness only, and it
#: reveals nothing beyond "a Jarvis core is running here".
PUBLIC_PATHS = frozenset({"/health", "/openapi.json", "/docs", "/redoc"})

#: Tauri's webview origins, plus the Vite dev server.
ALLOWED_ORIGINS = frozenset(
    {
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
        "http://localhost:5183",
        "http://127.0.0.1:5183",
    }
)


def mint_token() -> str:
    return pysecrets.token_urlsafe(TOKEN_BYTES)


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _origin_ok(origin: str | None) -> bool:
    """No Origin header (a native client) is fine; a wrong one is not."""
    if not origin:
        return True
    if origin in ALLOWED_ORIGINS:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return parsed.hostname in {"localhost", "127.0.0.1", "tauri.localhost"}


def _apply_cors(response: Response, origin: str | None) -> None:
    """Echo an already-validated origin back so the browser releases the body.

    Only called after `_origin_ok` has passed, so this never widens access; it
    reflects one specific allowed origin rather than using a wildcard, and keeps
    `Vary: Origin` so nothing caches the wrong answer.
    """
    if not origin:
        return
    response.headers["access-control-allow-origin"] = origin
    response.headers["access-control-allow-methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["access-control-allow-headers"] = "authorization, content-type"
    response.headers["access-control-max-age"] = "600"
    response.headers["vary"] = "Origin"


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        origin = request.headers.get("origin")
        origin_allowed = _origin_ok(origin)

        if not origin_allowed:
            log.warning("rejected cross-origin request", origin=origin)
            return JSONResponse(
                {"code": "jarvis.auth.origin", "message": "Cross-origin requests are refused."},
                status_code=status.HTTP_403_FORBIDDEN,
            )

        # Preflight carries no Authorization header by design, so it is answered
        # before the token check. It reveals nothing beyond which methods exist.
        if request.method == "OPTIONS" and origin is not None:
            response: Response = Response(status_code=status.HTTP_204_NO_CONTENT)
            _apply_cors(response, origin)
            return response

        if request.url.path in PUBLIC_PATHS:
            response = await call_next(request)
            _apply_cors(response, origin)
            return response

        header = request.headers.get("authorization", "")
        presented = header[7:] if header.lower().startswith("bearer ") else ""
        if not presented or not _constant_time_eq(presented, self.token):
            response = JSONResponse(
                {
                    "code": "jarvis.auth.token",
                    "message": "Missing or invalid API token.",
                },
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
            _apply_cors(response, origin)
            return response

        response = await call_next(request)
        _apply_cors(response, origin)
        return response


async def authorise_websocket(ws: WebSocket, token: str) -> bool:
    """Check a WebSocket upgrade.

    Browsers cannot set headers on a WebSocket handshake, so the token may also
    arrive as a query parameter. That is acceptable here because the URL never
    leaves the machine and the token dies with the process.
    """
    if not _origin_ok(ws.headers.get("origin")):
        await ws.close(code=4403, reason="cross-origin refused")
        return False

    header = ws.headers.get("authorization", "")
    presented = header[7:] if header.lower().startswith("bearer ") else ""
    if not presented:
        presented = ws.query_params.get("token", "")

    if not presented or not _constant_time_eq(presented, token):
        await ws.close(code=4401, reason="invalid token")
        return False
    return True

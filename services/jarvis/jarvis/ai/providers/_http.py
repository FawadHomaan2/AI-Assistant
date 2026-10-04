"""Shared HTTP plumbing for network-backed providers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from jarvis.util.errors import ProviderAuthError, ProviderError, ProviderUnavailable


def client(base_url: str, timeout: float, headers: dict[str, str]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers=headers,
        follow_redirects=False,
    )


def raise_for_status(response: httpx.Response, provider: str, body: str = "") -> None:
    """Translate an HTTP error into a typed, explainable failure."""
    status = response.status_code
    if status < 400:
        return
    detail = (body or response.text or "").strip()[:400]
    if status in (401, 403):
        raise ProviderAuthError(
            f"{provider} rejected the credentials ({status}). Check the API key in Settings.",
            status=status,
            detail=detail,
        )
    if status == 404:
        raise ProviderError(
            f"{provider} returned 404. The model name or endpoint URL is probably wrong.",
            status=status,
            detail=detail,
        )
    if status == 429:
        raise ProviderUnavailable(
            f"{provider} is rate-limiting this request (429). Try again shortly.",
            status=status,
            detail=detail,
        )
    if status >= 500:
        raise ProviderUnavailable(
            f"{provider} returned a server error ({status}).", status=status, detail=detail
        )
    raise ProviderError(
        f"{provider} rejected the request ({status}).", status=status, detail=detail
    )


def wrap_transport_error(exc: Exception, provider: str, url: str) -> ProviderUnavailable:
    if isinstance(exc, httpx.ConnectError):
        return ProviderUnavailable(
            f"Could not reach {provider} at {url}. Is it running, and is the URL right?",
            url=url,
        )
    if isinstance(exc, httpx.TimeoutException):
        return ProviderUnavailable(f"{provider} timed out at {url}.", url=url)
    return ProviderUnavailable(f"{provider} connection failed: {exc}", url=url)


async def sse_lines(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """Parse a text/event-stream into (event, data) pairs.

    Tolerates servers that omit `event:` (most OpenAI-compatible ones do) and
    skips comments and keep-alives.
    """
    event = "message"
    async for raw in response.aiter_lines():
        line = raw.rstrip("\r")
        if not line:
            event = "message"
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if line.startswith("data:"):
            yield event, line[5:].strip()


def parse_json(data: str, provider: str) -> dict[str, Any]:
    try:
        parsed: Any = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{provider} sent malformed JSON: {data[:200]}") from exc
    if not isinstance(parsed, dict):
        raise ProviderError(f"{provider} sent an unexpected payload type: {type(parsed).__name__}")
    return parsed

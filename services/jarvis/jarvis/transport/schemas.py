"""Request and response models for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32_000)
    session_id: str | None = None


class SessionOut(BaseModel):
    id: str
    started_at: str
    title: str | None = None
    mode: str = "guarded"
    ended_at: str | None = None


class TurnOut(BaseModel):
    id: str
    role: str
    content: str
    created_at: str
    provider: str | None = None
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None
    error_code: str | None = None


class ProviderOut(BaseModel):
    name: str
    kind: str
    model: str
    streaming: bool
    tools: bool
    is_cloud: bool
    configured: bool
    detail: str


class HealthOut(BaseModel):
    status: str
    version: str
    schema_version: int
    mode: str
    emergency_stop: bool
    default_provider: str
    credential_store: dict[str, Any]


class ErrorOut(BaseModel):
    code: str
    message: str
    context: dict[str, Any] = Field(default_factory=dict)

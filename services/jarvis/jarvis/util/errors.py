"""Typed errors.

Every failure that reaches the user carries a stable `code` the UI can branch
on and a `message` written for a person to read. Rule 23 of the project brief:
failures are explained, never swallowed.
"""

from __future__ import annotations

from typing import Any


class JarvisError(Exception):
    """Base class. `code` is stable API; `message` is human-facing."""

    code = "jarvis.error"
    http_status = 500

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "context": self.context}


class ConfigError(JarvisError):
    code = "jarvis.config"
    http_status = 500


class ProviderError(JarvisError):
    """An AI provider failed. Carries enough detail to tell the user why."""

    code = "jarvis.provider"
    http_status = 502


class ProviderNotConfigured(ProviderError):
    code = "jarvis.provider.not_configured"
    http_status = 409


class ProviderUnavailable(ProviderError):
    """Reachability failure — server down, DNS, timeout."""

    code = "jarvis.provider.unavailable"
    http_status = 503


class ProviderAuthError(ProviderError):
    code = "jarvis.provider.auth"
    http_status = 401


class PrivacyViolation(JarvisError):
    """A privacy class was about to leave the machine that may never leave.

    Raised rather than filtered, deliberately: a redaction filter can be
    mis-tuned, a type error cannot.
    """

    code = "jarvis.privacy.violation"
    http_status = 403


class Cancelled(JarvisError):
    code = "jarvis.cancelled"
    http_status = 499


class EmergencyStopped(Cancelled):
    code = "jarvis.emergency_stop"
    http_status = 409


class PlatformUnsupported(JarvisError):
    """A Windows-only capability was called on another platform."""

    code = "jarvis.platform_unsupported"
    http_status = 501

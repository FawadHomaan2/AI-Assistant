"""Strip secrets before anything is logged, stored, or sent to a cloud model.

This is defence in depth, not the primary control. The primary control is that
secrets live in the OS credential store and are never put in log calls in the
first place. This catches the mistakes.

Deliberately conservative: it over-redacts rather than under-redacts, because a
false positive costs a reviewer some confusion and a false negative leaks a key.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[redacted]"

# Keys whose values are never safe to record, matched case-insensitively against
# the whole key or any underscore/dash-separated part of it.
_SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "apikey",
        "api_key",
        "access_token",
        "refresh_token",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "private_key",
        "privatekey",
        "session_key",
        "cookie",
        "bearer",
        "signature",
        "client_secret",
        "passphrase",
        "pin",
    }
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Provider key formats, most specific first.
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), MASK),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), MASK),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"AKIA[0-9A-Z]{16}"), MASK),
    # Authorization headers in a serialised request.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 " + MASK),
    # key=value / "key": "value" forms that slipped into a plain string.
    (
        re.compile(
            r"(?i)\b(" + "|".join(sorted(_SECRET_KEYS)) + r")"
            r'(\s*[=:]\s*)(["\']?)([^\s"\',;}]{4,})(\3)'
        ),
        r"\1\2\3" + MASK + r"\5",
    ),
    # JWTs.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), MASK),
    # Email addresses: not secret, but not ours to scatter through logs either.
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
)

# A long opaque blob is probably a key even if nothing named it.
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9+/_\-]{40,}={0,2}\b")


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SECRET_KEYS:
        return True
    parts = re.split(r"[_\-.]", lowered)
    return any(part in _SECRET_KEYS for part in parts) or any(
        lowered.endswith(suffix) for suffix in ("_key", "_token", "_secret", "_password")
    )


def redact_text(value: str) -> str:
    """Mask secrets inside a free-form string."""
    out = value
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    # Only sweep for high-entropy blobs once the named patterns are gone, so we
    # do not mangle something already masked.
    out = _HIGH_ENTROPY.sub(lambda m: MASK if _looks_opaque(m.group(0)) else m.group(0), out)
    return out


def _looks_opaque(token: str) -> bool:
    """Mixed-case-and-digit runs look like keys; prose and paths do not."""
    if "/" in token or "\\" in token:
        return False
    has_digit = any(c.isdigit() for c in token)
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    return has_digit and has_upper and has_lower


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact a structure, preserving its shape.

    Depth-limited so a cyclic or pathological structure cannot hang logging.
    """
    if _depth > 12:
        return "[truncated]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_secret_key(key):
                out[key] = MASK
            else:
                out[key] = redact(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        rendered = [redact(item, _depth + 1) for item in value]
        return type(value)(rendered) if not isinstance(value, set) else set(rendered)
    return value


def redaction_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: redact every field of every log record."""
    cleaned = redact(event_dict)
    assert isinstance(cleaned, dict)  # redact() preserves the input's shape
    return cleaned

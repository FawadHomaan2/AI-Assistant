"""Credential storage.

On Windows this is the Credential Manager (DPAPI-protected, per-user) through
`keyring`. Secrets are never written to config.toml, never logged, and never
read back into the UI — Settings shows "configured", not the value.

If `keyring` is absent or no backend is usable (headless CI, a container), this
reports that honestly instead of silently falling back to a file, which would
turn a secure store into a plaintext one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger

log = get_logger(__name__)

SERVICE = "Jarvis"


class CredentialStoreUnavailable(JarvisError):
    code = "jarvis.credentials.unavailable"
    http_status = 503


@dataclass(frozen=True)
class StoreStatus:
    available: bool
    backend: str
    detail: str


def _keyring() -> Any:
    """Return the keyring module. Typed as Any: the package ships no stubs."""
    try:
        import keyring
    except ImportError as exc:
        raise CredentialStoreUnavailable(
            "The `keyring` package is not installed, so API keys cannot be stored "
            "securely. Install the 'secrets' extra. Jarvis will not fall back to "
            "writing keys to disk."
        ) from exc
    return keyring


def status() -> StoreStatus:
    """Describe the credential backend without touching any secret."""
    try:
        keyring = _keyring()
    except CredentialStoreUnavailable as exc:
        return StoreStatus(False, "none", exc.message)
    try:
        backend = keyring.get_keyring()
        name = type(backend).__name__
        if "fail" in name.lower():
            return StoreStatus(False, name, "No usable credential backend on this system.")
        return StoreStatus(True, name, "Credential store ready.")
    except Exception as exc:
        return StoreStatus(False, "unknown", f"Credential backend probe failed: {exc}")


def get(name: str) -> str | None:
    """Fetch a secret. Returns None when it is simply not set."""
    if not name:
        return None
    keyring = _keyring()
    try:
        value: str | None = keyring.get_password(SERVICE, name)
        return value
    except Exception as exc:
        raise CredentialStoreUnavailable(f"Could not read credential {name!r}: {exc}") from exc


def set_secret(name: str, value: str) -> None:
    if not name:
        raise ValueError("credential name is required")
    keyring = _keyring()
    try:
        keyring.set_password(SERVICE, name, value)
    except Exception as exc:
        raise CredentialStoreUnavailable(f"Could not store credential {name!r}: {exc}") from exc
    # The name, never the value.
    log.info("credential stored", credential_name=name)


def delete(name: str) -> bool:
    keyring = _keyring()
    try:
        keyring.delete_password(SERVICE, name)
    except Exception:
        return False
    log.info("credential deleted", credential_name=name)
    return True


def has(name: str) -> bool:
    """Whether a credential exists, without revealing it."""
    try:
        return bool(get(name))
    except CredentialStoreUnavailable:
        return False

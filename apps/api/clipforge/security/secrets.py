from __future__ import annotations

import re
from typing import Literal, Protocol, TypedDict, cast

import keyring
from keyring.errors import PasswordDeleteError

SecretName = Literal[
    "OPENAI_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "PEXELS_API_KEY",
    "EUROPEANA_API_KEY",
    "YOUTUBE_OAUTH_CLIENT_ID",
    "YOUTUBE_OAUTH_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
    "TIKTOK_CLIENT_KEY",
    "TIKTOK_CLIENT_SECRET",
    "META_APP_ID",
    "META_APP_SECRET",
]
SUPPORTED_SECRET_NAMES: tuple[SecretName, ...] = (
    "OPENAI_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "PEXELS_API_KEY",
    "EUROPEANA_API_KEY",
    # YouTube OAuth client and the connected channel's refresh token live only
    # in the OS keyring; the refresh token is never copied into Settings.
    "YOUTUBE_OAUTH_CLIENT_ID",
    "YOUTUBE_OAUTH_CLIENT_SECRET",
    # Legacy single-channel refresh token (before multi-account publishing).
    # Read only to migrate the account it belonged to; never written again.
    "YOUTUBE_REFRESH_TOKEN",
    # Publishing developer apps (TikTok Content Posting, Meta / Instagram).
    "TIKTOK_CLIENT_KEY",
    "TIKTOK_CLIENT_SECRET",
    "META_APP_ID",
    "META_APP_SECRET",
)

# Per-account credentials are namespaced "<BASE>:<account id>" so every
# connected publishing account has its own keyring entry; there is no global
# token per platform.
AccountSecretBase = Literal[
    "YOUTUBE_REFRESH_TOKEN",
    "TIKTOK_REFRESH_TOKEN",
    "INSTAGRAM_TOKEN",
]
ACCOUNT_SECRET_BASES: tuple[AccountSecretBase, ...] = (
    "YOUTUBE_REFRESH_TOKEN",
    "TIKTOK_REFRESH_TOKEN",
    "INSTAGRAM_TOKEN",
)
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9-]{8,64}")


def account_secret_name(base: AccountSecretBase, account_id: str) -> str:
    """The keyring entry of one account's credential, e.g. ``TIKTOK_REFRESH_TOKEN:<id>``."""
    if base not in ACCOUNT_SECRET_BASES:
        raise ValueError(f"Unsupported account secret: {base}")
    if not _ACCOUNT_ID.fullmatch(account_id or ""):
        raise ValueError("Invalid account id for a secret name")
    return f"{base}:{account_id}"


class KeyringBackend(Protocol):
    def set_password(self, service: str, username: str, password: str) -> None: ...

    def get_password(self, service: str, username: str) -> str | None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class SecretMetadata(TypedDict):
    configured: bool
    last_four: str | None


class SecretStore:
    """Store supported provider API keys in the operating system keyring."""

    SERVICE_NAME = "ClipForge"

    def __init__(self, backend: KeyringBackend | None = None) -> None:
        self._backend = (
            backend if backend is not None else cast(KeyringBackend, keyring.get_keyring())
        )

    def set_account_secret(self, base: AccountSecretBase, account_id: str, value: str) -> None:
        self.set_secret(account_secret_name(base, account_id), value)  # type: ignore[arg-type]

    def get_account_secret(self, base: AccountSecretBase, account_id: str) -> str | None:
        return self.get_secret(account_secret_name(base, account_id))  # type: ignore[arg-type]

    def delete_account_secret(self, base: AccountSecretBase, account_id: str) -> None:
        self.delete_secret(account_secret_name(base, account_id))  # type: ignore[arg-type]

    def set_secret(self, name: SecretName, value: str) -> None:
        """Store a non-empty secret after removing accidental surrounding whitespace."""
        self._validate_name(name)
        clean = value.strip()
        if not clean:
            raise ValueError("Secret value cannot be empty")
        self._backend.set_password(self.SERVICE_NAME, name, clean)

    def get_secret(self, name: SecretName) -> str | None:
        """Retrieve a secret without exposing it through logs or metadata."""
        self._validate_name(name)
        value = self._backend.get_password(self.SERVICE_NAME, name)
        if value is None:
            return None
        clean = value.strip()
        return clean or None

    def delete_secret(self, name: SecretName) -> None:
        """Remove a secret, treating an already-missing entry as deleted."""
        self._validate_name(name)
        try:
            self._backend.delete_password(self.SERVICE_NAME, name)
        except PasswordDeleteError:
            pass

    def get_metadata(self, name: SecretName) -> SecretMetadata:
        """Return only the safe fields needed to describe a stored secret."""
        value = self.get_secret(name)
        if value is None:
            return {"configured": False, "last_four": None}
        return {
            "configured": True,
            "last_four": value[-4:] if len(value) >= 8 else "****",
        }

    @staticmethod
    def _validate_name(name: str) -> None:
        if name in SUPPORTED_SECRET_NAMES:
            return
        base, separator, account_id = name.partition(":")
        if separator and base in ACCOUNT_SECRET_BASES and _ACCOUNT_ID.fullmatch(account_id):
            return
        raise ValueError(f"Unsupported secret name: {name}")

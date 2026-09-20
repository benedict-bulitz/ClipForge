from typing import Literal, Protocol, TypedDict, cast

import keyring
from keyring.errors import PasswordDeleteError

SecretName = Literal["OPENAI_API_KEY", "BRAVE_SEARCH_API_KEY", "PEXELS_API_KEY"]
SUPPORTED_SECRET_NAMES: tuple[SecretName, ...] = (
    "OPENAI_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "PEXELS_API_KEY",
)


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
        if name not in SUPPORTED_SECRET_NAMES:
            raise ValueError(f"Unsupported secret name: {name}")

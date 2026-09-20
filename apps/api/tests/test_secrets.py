from unittest.mock import MagicMock

from keyring.errors import PasswordDeleteError

from clipforge.config import Settings, resolve_settings
from clipforge.security.secrets import SecretStore


def test_secret_store_stores_retrieves_and_masks_metadata():
    backend = MagicMock()
    backend.get_password.return_value = "  sk-test-1234  "
    store = SecretStore(backend=backend)

    store.set_secret("OPENAI_API_KEY", "  sk-test-1234  ")

    backend.set_password.assert_called_once_with("ClipForge", "OPENAI_API_KEY", "sk-test-1234")
    assert store.get_secret("OPENAI_API_KEY") == "sk-test-1234"
    assert store.get_metadata("OPENAI_API_KEY") == {
        "configured": True,
        "last_four": "1234",
    }


def test_secret_store_deletes_secrets_idempotently():
    backend = MagicMock()
    store = SecretStore(backend=backend)

    store.delete_secret("PEXELS_API_KEY")
    backend.delete_password.assert_called_once_with("ClipForge", "PEXELS_API_KEY")

    backend.delete_password.side_effect = PasswordDeleteError("Secret does not exist")
    store.delete_secret("PEXELS_API_KEY")


def test_secret_store_takes_precedence_over_dotenv(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "BRAVE_SEARCH_API_KEY", "PEXELS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "OPENAI_API_KEY=env-openai\n"
        "BRAVE_SEARCH_API_KEY=env-brave\n"
        "PEXELS_API_KEY=env-pexels\n",
        encoding="utf-8",
    )
    stored = {
        "OPENAI_API_KEY": "keyring-openai",
        "BRAVE_SEARCH_API_KEY": "keyring-brave",
        "PEXELS_API_KEY": "keyring-pexels",
    }
    backend = MagicMock()
    backend.get_password.side_effect = lambda _service, name: stored.get(name)

    resolved = resolve_settings(
        Settings(_env_file=dotenv),
        SecretStore(backend=backend),
    )

    assert resolved.openai_api_key == "keyring-openai"
    assert resolved.brave_search_api_key == "keyring-brave"
    assert resolved.pexels_api_key == "keyring-pexels"


def test_dotenv_remains_the_fallback(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "BRAVE_SEARCH_API_KEY", "PEXELS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "OPENAI_API_KEY=env-openai\n"
        "BRAVE_SEARCH_API_KEY=env-brave\n"
        "PEXELS_API_KEY=env-pexels\n",
        encoding="utf-8",
    )
    backend = MagicMock()
    backend.get_password.return_value = None

    resolved = resolve_settings(
        Settings(_env_file=dotenv),
        SecretStore(backend=backend),
    )

    assert resolved.openai_api_key == "env-openai"
    assert resolved.brave_search_api_key == "env-brave"
    assert resolved.pexels_api_key == "env-pexels"

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class TestKeyring(KeyringBackend):
    """In-memory keyring used before any ClipForge application module is imported."""

    priority = 1

    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.secrets.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.secrets[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.secrets[(service, username)]
        except KeyError as exc:
            raise PasswordDeleteError("Secret does not exist") from exc


TEST_KEYRING = TestKeyring()
keyring.set_keyring(TEST_KEYRING)

from clipforge.config import get_settings
from clipforge.database import Base
from clipforge.voice_preview import reset_preview_rate_limits


@pytest.fixture(autouse=True)
def reset_test_services():
    TEST_KEYRING.secrets.clear()
    get_settings.cache_clear()
    reset_preview_rate_limits()
    yield
    TEST_KEYRING.secrets.clear()
    get_settings.cache_clear()
    reset_preview_rate_limits()


@pytest.fixture()
def test_keyring():
    return TEST_KEYRING


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def forbid_real_image_generation(monkeypatch):
    """No test may reach the paid OpenAI Images API; generators must be mocked."""
    from clipforge import image_generation

    calls: list[tuple] = []

    def forbidden(*args, **_kwargs):
        calls.append(args)
        raise AssertionError("Real OpenAI image generation is forbidden in the test suite.")

    monkeypatch.setattr(image_generation, "OPENAI_CLIENT_FACTORY", forbidden)
    return calls


@pytest.fixture(autouse=True)
def forbid_real_visual_translation(monkeypatch):
    """The fact -> visual translator must be mocked; no real worker-model calls."""
    from clipforge import visual_translation

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Real OpenAI visual translation is forbidden in the test suite.")

    visual_translation.clear_translation_cache()
    monkeypatch.setattr(visual_translation, "TRANSLATOR_CLIENT_FACTORY", forbidden)


@pytest.fixture(autouse=True)
def forbid_real_overlay_summaries(monkeypatch):
    """The overlay-copy summariser must be mocked; no real worker-model calls."""
    from clipforge import overlay_copy

    def forbidden(*_args, **_kwargs):
        # Never a network client: the deterministic relation builder is used.
        raise OSError("Real OpenAI overlay summaries are disabled in the test suite.")

    overlay_copy.clear_summary_cache()
    monkeypatch.setattr(overlay_copy, "SUMMARY_CLIENT_FACTORY", forbidden)

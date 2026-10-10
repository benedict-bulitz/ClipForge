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
def forbid_live_open_media(monkeypatch):
    """Contract tests inject MockTransport; the full suite never calls new APIs."""
    from clipforge import open_media

    def forbidden(*_args, **_kwargs):
        import httpx
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": [], "collection": {"items": []}, "items": [], "success": True}, request=request)))

    open_media.clear_search_cache()
    monkeypatch.setattr(open_media, "HTTP_CLIENT_FACTORY", forbidden)
    yield
    open_media.clear_search_cache()


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


@pytest.fixture(autouse=True)
def no_topic_warmup(monkeypatch):
    """App startup must not reach real discovery providers from the test suite."""
    from clipforge.topic_intelligence import service

    started: list[object] = []
    monkeypatch.setattr(service, "warm_pool_in_background", lambda *args, **kwargs: started.append(args))
    return started


@pytest.fixture(autouse=True)
def forbid_real_semantic_curation(monkeypatch):
    """Topic curation (question + judgement) must be faked; no real worker-model calls."""
    from clipforge.topic_intelligence import semantic

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Real OpenAI topic curation is forbidden in the test suite.")

    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", forbidden)


@pytest.fixture(autouse=True)
def no_publishing_scheduler(monkeypatch):
    """App startup must not start the background publication scheduler in tests."""
    from clipforge import main

    started: list[object] = []
    monkeypatch.setattr(main, "start_publishing_scheduler", lambda thread: started.append(thread) or thread)
    return started


@pytest.fixture(autouse=True)
def offline_script_story_editor(monkeypatch):
    """The AI script editor is never reached: it behaves as unavailable (deterministic fallback).

    Tests that exercise critic -> rewrite -> verifier inject a fake provider.
    """
    from clipforge import script_story_rewrite

    def offline(*_args, **_kwargs):
        raise script_story_rewrite.ScriptStoryProviderError("AI script editor is offline in the test suite")

    monkeypatch.setattr(script_story_rewrite.OpenAIScriptStoryProvider, "_parse", offline)


@pytest.fixture(autouse=True)
def forbid_paid_openai_transport(monkeypatch):
    """Block only live OpenAI HTTP traffic; leave QAC planning and mocked parsing intact."""
    import httpx

    send = httpx.Client.send

    def offline(client, request, *args, **kwargs):
        if request.url.host == "api.openai.com" and isinstance(client._transport, httpx.HTTPTransport):
            raise AssertionError("Live OpenAI HTTP traffic is forbidden in backend tests")
        return send(client, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", offline)


@pytest.fixture()
def legacy_without_qac(monkeypatch):
    """Explicit opt-in for pre-QAC regression cases; never used by contract integration tests."""
    monkeypatch.setattr("clipforge.pipeline.generate_contract", lambda *_a: None)

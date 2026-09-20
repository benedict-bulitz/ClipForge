from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient

from clipforge import config
from clipforge.config import Settings, get_settings, load_environment_settings
from clipforge.integrations import (
    PROVIDERS,
    ProviderValidator,
    ValidationResult,
    get_provider_validator,
    get_secret_store,
)
from clipforge.main import app
from clipforge.schemas import IntegrationKeyCreate, IntegrationProvider
from clipforge.security.secrets import SecretStore


class FakeValidator:
    def __init__(self) -> None:
        self.results = {
            provider: ValidationResult("connected", "Connection successful.")
            for provider in PROVIDERS
        }
        self.providers_tested: list[IntegrationProvider] = []

    def validate(self, provider: IntegrationProvider, _api_key: str) -> ValidationResult:
        self.providers_tested.append(provider)
        return self.results[provider]


@dataclass
class IntegrationHarness:
    client: TestClient
    store: SecretStore
    validator: FakeValidator
    environment_state: dict[str, Settings]

    def use_environment(self, settings: Settings) -> None:
        self.environment_state["settings"] = settings
        get_settings.cache_clear()


@pytest.fixture()
def integrations(monkeypatch, test_keyring):
    environment_state = {
        "settings": Settings(
            _env_file=None,
            openai_api_key=None,
            brave_search_api_key=None,
            pexels_api_key=None,
        )
    }
    store = SecretStore(backend=test_keyring)
    validator = FakeValidator()

    def environment_settings() -> Settings:
        return environment_state["settings"]

    monkeypatch.setattr(config, "load_environment_settings", environment_settings)
    app.dependency_overrides[load_environment_settings] = environment_settings
    app.dependency_overrides[get_secret_store] = lambda: store
    app.dependency_overrides[get_provider_validator] = lambda: validator
    get_settings.cache_clear()

    with TestClient(app) as client:
        yield IntegrationHarness(client, store, validator, environment_state)

    app.dependency_overrides.clear()


def _seed(store: SecretStore, provider: IntegrationProvider, suffix: str = "1234") -> None:
    store.set_secret(PROVIDERS[provider].secret_name, f"unit-{provider}-credential-{suffix}")
    get_settings.cache_clear()


def test_get_integrations_returns_only_safe_metadata(integrations):
    _seed(integrations.store, "openai", "1111")
    _seed(integrations.store, "brave", "2222")

    response = integrations.client.get("/api/settings/integrations")

    assert response.status_code == 200
    body = response.json()
    assert [item["provider"] for item in body] == ["openai", "brave", "pexels"]
    assert body[0] == {
        "provider": "openai",
        "configured": True,
        "last_four": "1111",
        "status": "configured",
        "source": "keyring",
        "message": None,
    }
    assert body[2]["status"] == "not_configured"
    assert "credential" not in response.text


def test_candidate_key_repr_is_redacted():
    candidate = "unit-candidate-credential-1234"

    payload = IntegrationKeyCreate(api_key=candidate)

    assert candidate not in repr(payload)


@pytest.mark.parametrize("provider", ["openai", "brave", "pexels"])
def test_saving_valid_provider_key_uses_secret_store_and_refreshes_cache(
    integrations,
    provider,
):
    candidate = f"unit-{provider}-credential-4321"

    response = integrations.client.post(
        f"/api/settings/integrations/{provider}",
        json={"api_key": candidate},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "connected"
    assert response.json()["last_four"] == "4321"
    assert response.json()["source"] == "keyring"
    assert candidate not in response.text
    assert integrations.store.get_metadata(PROVIDERS[provider].secret_name)["configured"] is True
    assert getattr(get_settings(), PROVIDERS[provider].settings_field).endswith("4321")


@pytest.mark.parametrize("provider", ["openai", "brave", "pexels"])
def test_invalid_provider_key_is_rejected_without_saving(integrations, provider):
    candidate = f"unit-{provider}-invalid-0000"
    integrations.validator.results[provider] = ValidationResult(
        "invalid_credentials",
        "The provider rejected the API key.",
    )

    response = integrations.client.post(
        f"/api/settings/integrations/{provider}",
        json={"api_key": candidate},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["status"] == "invalid_credentials"
    assert integrations.store.get_metadata(PROVIDERS[provider].secret_name)["configured"] is False
    assert candidate not in response.text


def test_network_failure_is_distinct_from_invalid_credentials(integrations):
    integrations.validator.results["brave"] = ValidationResult(
        "network_error",
        "The provider could not be reached.",
    )

    response = integrations.client.post(
        "/api/settings/integrations/brave",
        json={"api_key": "unit-brave-unreachable-5555"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "provider": "brave",
        "status": "network_error",
        "message": "The provider could not be reached.",
    }
    assert integrations.store.get_metadata("BRAVE_SEARCH_API_KEY")["configured"] is False


def test_connection_test_succeeds_without_modifying_credentials(integrations):
    _seed(integrations.store, "pexels", "6789")

    response = integrations.client.post("/api/settings/integrations/pexels/test")

    assert response.status_code == 200
    assert response.json()["status"] == "connected"
    assert response.json()["last_four"] == "6789"
    assert integrations.validator.providers_tested == ["pexels"]


def test_connection_test_reports_not_configured_without_network_call(integrations):
    response = integrations.client.post("/api/settings/integrations/openai/test")

    assert response.status_code == 200
    assert response.json()["status"] == "not_configured"
    assert response.json()["configured"] is False
    assert integrations.validator.providers_tested == []


def test_delete_removes_only_keyring_value_and_refreshes_cache(integrations):
    _seed(integrations.store, "openai", "1357")

    response = integrations.client.delete("/api/settings/integrations/openai")

    assert response.status_code == 200
    assert response.json()["status"] == "not_configured"
    assert integrations.store.get_metadata("OPENAI_API_KEY")["configured"] is False
    assert get_settings().openai_api_key is None


def test_delete_reveals_environment_fallback(integrations):
    integrations.use_environment(
        Settings(_env_file=None, openai_api_key="unit-env-openai-2468")
    )
    _seed(integrations.store, "openai", "1357")

    response = integrations.client.delete("/api/settings/integrations/openai")

    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert response.json()["last_four"] == "2468"
    assert response.json()["source"] == "environment"
    assert get_settings().openai_api_key.endswith("2468")


def test_env_import_uses_env_source_preserves_file_and_refreshes_cache(
    integrations,
    tmp_path,
    monkeypatch,
):
    for name in ("OPENAI_API_KEY", "BRAVE_SEARCH_API_KEY", "PEXELS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    dotenv = tmp_path / ".env"
    original = (
        "OPENAI_API_KEY=unit-env-openai-1111\n"
        "BRAVE_SEARCH_API_KEY=unit-env-brave-2222\n"
        "PEXELS_API_KEY=unit-env-pexels-3333\n"
        "UNRELATED_SETTING=preserve-me\n"
    )
    dotenv.write_text(original, encoding="utf-8")
    integrations.use_environment(Settings(_env_file=dotenv))

    response = integrations.client.post("/api/settings/integrations/import-env")

    assert response.status_code == 200
    assert [item["result"] for item in response.json()["results"]] == [
        "imported",
        "imported",
        "imported",
    ]
    assert [item["last_four"] for item in response.json()["results"]] == [
        "1111",
        "2222",
        "3333",
    ]
    assert "credential" not in response.text
    assert "unit-env" not in response.text
    assert dotenv.read_text(encoding="utf-8") == original
    assert get_settings().openai_api_key.endswith("1111")
    assert get_settings().brave_search_api_key.endswith("2222")
    assert get_settings().pexels_api_key.endswith("3333")


def test_env_import_skips_existing_keyring_value(integrations):
    integrations.use_environment(
        Settings(_env_file=None, pexels_api_key="unit-env-pexels-1111")
    )
    _seed(integrations.store, "pexels", "9999")

    response = integrations.client.post("/api/settings/integrations/import-env")

    result = next(
        item for item in response.json()["results"] if item["provider"] == "pexels"
    )
    assert result["result"] == "skipped"
    assert result["status"] == "configured"
    assert result["last_four"] == "9999"


def test_env_import_skips_invalid_value_without_exposing_or_saving_it(integrations):
    candidate = "unit-env-brave-invalid-0000"
    integrations.use_environment(
        Settings(_env_file=None, brave_search_api_key=candidate)
    )
    integrations.validator.results["brave"] = ValidationResult(
        "invalid_credentials",
        "The provider rejected the API key.",
    )

    response = integrations.client.post("/api/settings/integrations/import-env")

    result = next(
        item for item in response.json()["results"] if item["provider"] == "brave"
    )
    assert result["result"] == "skipped"
    assert result["status"] == "invalid_credentials"
    assert integrations.store.get_metadata("BRAVE_SEARCH_API_KEY")["configured"] is False
    assert candidate not in response.text


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, "invalid_credentials"),
        (403, "provider_error"),
        (429, "rate_limited"),
        (503, "provider_error"),
    ],
)
def test_provider_validator_classifies_http_failures(status_code, expected):
    transport = httpx.MockTransport(lambda request: httpx.Response(status_code, request=request))
    with httpx.Client(transport=transport) as client:
        result = ProviderValidator(client).validate("openai", "unit-test-value")

    assert result.status == expected


def test_provider_validator_classifies_timeouts_as_network_errors():
    def timeout(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        result = ProviderValidator(client).validate("brave", "unit-test-value")

    assert result.status == "network_error"

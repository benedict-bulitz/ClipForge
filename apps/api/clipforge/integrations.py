from dataclasses import dataclass
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from keyring.errors import KeyringError

from .config import (
    Settings,
    get_settings,
    load_environment_settings,
    refresh_settings,
)
from .schemas import (
    EnvImportRead,
    EnvImportResult,
    IntegrationKeyCreate,
    IntegrationProvider,
    IntegrationRead,
    IntegrationStatus,
)
from .security.secrets import SecretName, SecretStore

ValidationStatus = Literal[
    "connected",
    "invalid_credentials",
    "rate_limited",
    "network_error",
    "provider_error",
]


@dataclass(frozen=True)
class ProviderConfig:
    secret_name: SecretName
    settings_field: str
    url: str


@dataclass(frozen=True)
class ValidationResult:
    status: ValidationStatus
    message: str


PROVIDERS: dict[IntegrationProvider, ProviderConfig] = {
    "openai": ProviderConfig(
        secret_name="OPENAI_API_KEY",
        settings_field="openai_api_key",
        url="https://api.openai.com/v1/models",
    ),
    "brave": ProviderConfig(
        secret_name="BRAVE_SEARCH_API_KEY",
        settings_field="brave_search_api_key",
        url="https://api.search.brave.com/res/v1/web/search",
    ),
    "pexels": ProviderConfig(
        secret_name="PEXELS_API_KEY",
        settings_field="pexels_api_key",
        url="https://api.pexels.com/v1/curated",
    ),
}


class ProviderValidator:
    """Validate provider credentials with small, read-only API requests."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def validate(self, provider: IntegrationProvider, api_key: str) -> ValidationResult:
        try:
            if self._client is not None:
                response = self._request(self._client, provider, api_key)
            else:
                with httpx.Client(timeout=6, follow_redirects=False) as client:
                    response = self._request(client, provider, api_key)
        except httpx.TimeoutException:
            return ValidationResult("network_error", "The provider request timed out.")
        except httpx.RequestError:
            return ValidationResult("network_error", "The provider could not be reached.")

        if 200 <= response.status_code < 300:
            return ValidationResult("connected", "Connection successful.")
        if response.status_code == 401:
            return ValidationResult("invalid_credentials", "The provider rejected the API key.")
        if response.status_code == 429:
            return ValidationResult("rate_limited", "The provider rate limit was reached.")
        return ValidationResult("provider_error", "The provider could not validate the API key.")

    @staticmethod
    def _request(
        client: httpx.Client,
        provider: IntegrationProvider,
        api_key: str,
    ) -> httpx.Response:
        config = PROVIDERS[provider]
        headers = {"Accept": "application/json"}
        params: dict[str, str | int] | None = None
        if provider == "openai":
            headers["Authorization"] = f"Bearer {api_key}"
        elif provider == "brave":
            headers["X-Subscription-Token"] = api_key
            params = {"q": "ClipForge", "count": 1}
        else:
            headers["Authorization"] = api_key
            params = {"per_page": 1}
        return client.get(config.url, headers=headers, params=params)


def get_secret_store() -> SecretStore:
    return SecretStore()


def get_provider_validator() -> ProviderValidator:
    return ProviderValidator()


SettingsDep = Annotated[Settings, Depends(get_settings)]
EnvironmentSettingsDep = Annotated[Settings, Depends(load_environment_settings)]
SecretStoreDep = Annotated[SecretStore, Depends(get_secret_store)]
ValidatorDep = Annotated[ProviderValidator, Depends(get_provider_validator)]

router = APIRouter(prefix="/api/settings/integrations", tags=["settings"])


def _stored_secret(store: SecretStore, name: SecretName) -> str | None:
    try:
        return store.get_secret(name)
    except KeyringError:
        return None


def _metadata(
    provider: IntegrationProvider,
    settings: Settings,
    store: SecretStore,
    *,
    status_override: IntegrationStatus | None = None,
    message: str | None = None,
) -> IntegrationRead:
    config = PROVIDERS[provider]
    stored = _stored_secret(store, config.secret_name)
    effective = getattr(settings, config.settings_field)
    value = stored or effective
    source = "keyring" if stored else ("environment" if effective else None)
    configured = bool(value)
    return IntegrationRead(
        provider=provider,
        configured=configured,
        last_four=value[-4:] if value and len(value) >= 8 else ("****" if value else None),
        status=status_override or ("configured" if configured else "not_configured"),
        source=source,
        message=message,
    )


def _safe_candidate(payload: IntegrationKeyCreate) -> str:
    candidate = payload.api_key.get_secret_value().strip()
    if not candidate or len(candidate) > 4_096:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "status": "invalid_credentials",
                "message": "A non-empty API key is required.",
            },
        )
    return candidate


def _raise_validation_error(
    provider: IntegrationProvider,
    result: ValidationResult,
) -> None:
    status_code = {
        "invalid_credentials": status.HTTP_400_BAD_REQUEST,
        "rate_limited": status.HTTP_429_TOO_MANY_REQUESTS,
        "network_error": status.HTTP_503_SERVICE_UNAVAILABLE,
        "provider_error": status.HTTP_503_SERVICE_UNAVAILABLE,
    }[result.status]
    raise HTTPException(
        status_code=status_code,
        detail={
            "provider": provider,
            "status": result.status,
            "message": result.message,
        },
    )


def _storage_error(provider: IntegrationProvider) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "provider": provider,
            "status": "storage_error",
            "message": "Secure credential storage is unavailable.",
        },
    )


@router.get("", response_model=list[IntegrationRead])
def list_integrations(settings: SettingsDep, store: SecretStoreDep) -> list[IntegrationRead]:
    return [_metadata(provider, settings, store) for provider in PROVIDERS]


def save_integration(
    provider: IntegrationProvider,
    payload: IntegrationKeyCreate,
    store: SecretStore,
    validator: ProviderValidator,
) -> IntegrationRead:
    candidate = _safe_candidate(payload)
    validation = validator.validate(provider, candidate)
    if validation.status != "connected":
        _raise_validation_error(provider, validation)
    try:
        store.set_secret(PROVIDERS[provider].secret_name, candidate)
    except KeyringError as exc:
        raise _storage_error(provider) from exc
    settings = refresh_settings()
    return _metadata(
        provider,
        settings,
        store,
        status_override="connected",
        message=validation.message,
    )


@router.post("/openai", response_model=IntegrationRead)
def save_openai(
    payload: IntegrationKeyCreate,
    store: SecretStoreDep,
    validator: ValidatorDep,
) -> IntegrationRead:
    return save_integration("openai", payload, store, validator)


@router.post("/brave", response_model=IntegrationRead)
def save_brave(
    payload: IntegrationKeyCreate,
    store: SecretStoreDep,
    validator: ValidatorDep,
) -> IntegrationRead:
    return save_integration("brave", payload, store, validator)


@router.post("/pexels", response_model=IntegrationRead)
def save_pexels(
    payload: IntegrationKeyCreate,
    store: SecretStoreDep,
    validator: ValidatorDep,
) -> IntegrationRead:
    return save_integration("pexels", payload, store, validator)


def test_integration(
    provider: IntegrationProvider,
    settings: Settings,
    store: SecretStore,
    validator: ProviderValidator,
) -> IntegrationRead:
    config = PROVIDERS[provider]
    api_key = getattr(settings, config.settings_field)
    if not api_key:
        return _metadata(provider, settings, store, status_override="not_configured")
    result = validator.validate(provider, api_key)
    return _metadata(
        provider,
        settings,
        store,
        status_override=result.status,
        message=result.message,
    )


@router.post("/openai/test", response_model=IntegrationRead)
def test_openai(
    settings: SettingsDep,
    store: SecretStoreDep,
    validator: ValidatorDep,
) -> IntegrationRead:
    return test_integration("openai", settings, store, validator)


@router.post("/brave/test", response_model=IntegrationRead)
def test_brave(
    settings: SettingsDep,
    store: SecretStoreDep,
    validator: ValidatorDep,
) -> IntegrationRead:
    return test_integration("brave", settings, store, validator)


@router.post("/pexels/test", response_model=IntegrationRead)
def test_pexels(
    settings: SettingsDep,
    store: SecretStoreDep,
    validator: ValidatorDep,
) -> IntegrationRead:
    return test_integration("pexels", settings, store, validator)


def delete_integration(provider: IntegrationProvider, store: SecretStore) -> IntegrationRead:
    try:
        store.delete_secret(PROVIDERS[provider].secret_name)
    except KeyringError as exc:
        raise _storage_error(provider) from exc
    settings = refresh_settings()
    return _metadata(provider, settings, store)


@router.delete("/openai", response_model=IntegrationRead)
def delete_openai(store: SecretStoreDep) -> IntegrationRead:
    return delete_integration("openai", store)


@router.delete("/brave", response_model=IntegrationRead)
def delete_brave(store: SecretStoreDep) -> IntegrationRead:
    return delete_integration("brave", store)


@router.delete("/pexels", response_model=IntegrationRead)
def delete_pexels(store: SecretStoreDep) -> IntegrationRead:
    return delete_integration("pexels", store)


@router.post("/import-env", response_model=EnvImportRead)
def import_environment(
    environment: EnvironmentSettingsDep,
    store: SecretStoreDep,
    validator: ValidatorDep,
) -> EnvImportRead:
    outcomes: dict[IntegrationProvider, tuple[Literal["imported", "skipped"], IntegrationStatus]] = {}

    for provider, config in PROVIDERS.items():
        existing = _stored_secret(store, config.secret_name)
        candidate = getattr(environment, config.settings_field)
        if existing:
            outcomes[provider] = ("skipped", "configured")
            continue
        if not candidate or not candidate.strip():
            outcomes[provider] = ("skipped", "not_configured")
            continue

        validation = validator.validate(provider, candidate.strip())
        if validation.status != "connected":
            outcomes[provider] = ("skipped", validation.status)
            continue
        try:
            store.set_secret(config.secret_name, candidate)
        except KeyringError:
            outcomes[provider] = ("skipped", "storage_error")
            continue
        outcomes[provider] = ("imported", "connected")

    settings = refresh_settings()
    results: list[EnvImportResult] = []
    for provider, (action, outcome_status) in outcomes.items():
        metadata = _metadata(provider, settings, store, status_override=outcome_status)
        results.append(
            EnvImportResult(
                provider=provider,
                result=action,
                status=metadata.status,
                configured=metadata.configured,
                last_four=metadata.last_four,
                source=metadata.source,
            )
        )
    return EnvImportRead(results=results)

from __future__ import annotations

"""Classified, secret-free provider failures shared by the TikTok and Instagram boundaries."""

import logging
import re

# Codes that mean "the account must be reconnected" (never retried).
AUTH_CODES = frozenset({"auth_expired", "insufficient_scope", "not_connected", "client_not_configured"})
# Codes that may succeed later without any user action (bounded retry).
TRANSIENT_CODES = frozenset({"network_timeout", "network_error", "provider_error", "rate_limited", "media_not_ready"})


class PublishingApiError(RuntimeError):
    """A provider failure with a stable ``code`` and a user-safe message.

    ``retryable`` is True only for transient failures (timeouts, 5xx, rate
    limits); auth, permission, policy and media rejections are permanent.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        status_code: int | None = None,
        provider_code: str | None = None,
        provider_message: str | None = None,
        log_id: str | None = None,
        context: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = code in TRANSIENT_CODES if retryable is None else retryable
        self.status_code = status_code
        self.provider_code = provider_code
        # Troubleshooting metadata, already sanitized by ``safe_provider_text``:
        # the provider's own human-readable reason and its request id (TikTok
        # ``log_id``, which TikTok support asks for).
        self.provider_message = provider_message
        self.log_id = log_id
        self.context = context

    def scrub(self, *secrets: str | None) -> PublishingApiError:
        """Remove exact credential values a provider may have echoed back
        (defense in depth beyond the pattern-based ``safe_provider_text``)."""
        for secret in secrets:
            if not secret or len(secret) < 6:
                continue
            self.message = self.message.replace(secret, "[redacted]")
            if self.provider_message:
                self.provider_message = self.provider_message.replace(secret, "[redacted]")
        self.args = (self.message,)
        return self

    def diagnostics(self) -> dict[str, object]:
        """Secret-free facts about this failure, safe to persist and return from the API."""
        return {
            key: value
            for key, value in {
                "code": self.code,
                "provider_code": self.provider_code,
                "provider_message": self.provider_message,
                "log_id": self.log_id,
                "http_status": self.status_code,
                "context": self.context,
                "retryable": self.retryable,
            }.items()
            if value is not None
        }

    def __repr__(self) -> str:
        return f"PublishingApiError(code={self.code!r}, retryable={self.retryable}, provider_code={self.provider_code!r}, log_id={self.log_id!r})"


_SECRET_PARAMS = re.compile(
    r"((?:access_token|refresh_token|client_secret|client_key|code|code_verifier|fb_exchange_token|token|upload_id)=)[^&\s\"']+",
    flags=re.IGNORECASE,
)


# Bearer credentials, TikTok token formats (act./rft./cbt.), Meta EAA… user
# tokens and Google ya29./1// tokens wherever they appear in free text.
_SECRET_VALUES = re.compile(
    r"(?:\bBearer\s+\S+|\b(?:act|rft|cbt|clt)\.[A-Za-z0-9._~+/=-]{8,}|\bEAA[A-Za-z0-9]{16,}|\bya29\.[A-Za-z0-9._-]+|\b1//[A-Za-z0-9._-]{8,})",
)
# Any URL: TikTok upload URLs carry an upload token, OAuth URLs carry codes.
_URLS = re.compile(r"\bhttps?://\S+", flags=re.IGNORECASE)
_LOG_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def redact(text: str) -> str:
    return _SECRET_PARAMS.sub(r"\1[redacted]", text or "")


def safe_provider_text(text: object, *, limit: int = 300) -> str | None:
    """A provider's human-readable reason with every secret-shaped value removed.

    Removes URLs, bearer/OAuth tokens and ``key=value`` credentials, collapses
    whitespace and caps the length; returns None when nothing useful is left.
    """
    if not isinstance(text, str):
        return None
    clean = _URLS.sub("[url]", text)
    clean = _SECRET_VALUES.sub("[redacted]", clean)
    clean = redact(clean)
    clean = " ".join(clean.split())[:limit].strip()
    return clean or None


def safe_log_id(value: object) -> str | None:
    """A provider request id (TikTok ``log_id``): kept only if it looks like an id."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _LOG_ID.fullmatch(value) else None


class HttpLogRedactor(logging.Filter):
    """httpx logs every request URL; Graph/TikTok URLs may carry codes or tokens."""

    HOSTS = ("tiktokapis.com", "tiktok.com", "facebook.com", "instagram.com", "rupload.facebook.com")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        if any(host in message for host in self.HOSTS):
            record.msg = redact(message)
            record.args = ()
        return True


_REDACTOR = HttpLogRedactor()
for _name in ("httpx", "httpcore"):
    _logger = logging.getLogger(_name)
    if not any(isinstance(item, HttpLogRedactor) for item in _logger.filters):
        _logger.addFilter(_REDACTOR)

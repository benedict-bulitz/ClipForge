"""Classified, secret-free provider failures shared by the TikTok and Instagram boundaries."""
from __future__ import annotations

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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = code in TRANSIENT_CODES if retryable is None else retryable
        self.status_code = status_code
        self.provider_code = provider_code

    def __repr__(self) -> str:
        return f"PublishingApiError(code={self.code!r}, retryable={self.retryable})"


_SECRET_PARAMS = re.compile(
    r"((?:access_token|refresh_token|client_secret|client_key|code|code_verifier|fb_exchange_token|token|upload_id)=)[^&\s\"']+",
    flags=re.IGNORECASE,
)


def redact(text: str) -> str:
    return _SECRET_PARAMS.sub(r"\1[redacted]", text or "")


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

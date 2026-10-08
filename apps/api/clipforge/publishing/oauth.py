from __future__ import annotations

"""One-time OAuth sign-in state (CSRF ``state`` + PKCE verifier), in memory only."""

import secrets
import time
from dataclasses import dataclass
from threading import Lock

from .errors import PublishingApiError

PENDING_TTL_SECONDS = 600


@dataclass(frozen=True)
class PendingSignIn:
    platform: str
    verifier: str
    created: float
    account_id: str | None = None  # "Reconnect" target; None = "Add account"


_PENDING: dict[str, PendingSignIn] = {}
_LOCK = Lock()


def reset() -> None:
    with _LOCK:
        _PENDING.clear()


def begin(platform: str, *, account_id: str | None = None) -> tuple[str, str]:
    """``(state, verifier)``; the verifier never leaves this process except to the token endpoint."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)[:96]
    now = time.monotonic()
    with _LOCK:
        for key in [key for key, item in _PENDING.items() if now - item.created > PENDING_TTL_SECONDS]:
            _PENDING.pop(key, None)
        _PENDING[state] = PendingSignIn(platform=platform, verifier=verifier, created=now, account_id=account_id)
    return state, verifier


def complete(platform: str, state: str) -> PendingSignIn:
    with _LOCK:
        pending = _PENDING.pop(state, None)
    if pending is None or pending.platform != platform or time.monotonic() - pending.created > PENDING_TTL_SECONDS:
        raise PublishingApiError("invalid_state", "This sign-in link expired or was already used. Start again.", retryable=False)
    return pending

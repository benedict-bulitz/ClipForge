"""The one connection authority for every publishing platform.

Any number of accounts per platform; ``(platform, external_account_id)`` is
unique, so connecting the same channel/account again updates the existing row
(its ClipForge id, history and default flag stay stable).  Disconnecting keeps
the identity row (``status="disconnected"``) because uploads, schedules and
analytics refer to it; only the account's keyring entry is deleted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import PublishingAccount, PublishingPlatformConfig, YouTubeConnection

PLATFORMS = ("youtube", "instagram", "tiktok")
PLATFORM_LABELS = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok"}
ACTIVE_STATUSES = ("connected", "auth_expired", "error")
LEGACY_YOUTUBE_SLOT = "primary"


@dataclass(frozen=True)
class AccountIdentity:
    """What a provider says about a signed-in account (never credentials)."""

    platform: str
    external_account_id: str
    display_name: str
    handle: str | None = None
    avatar_url: str | None = None
    granted_scopes: tuple[str, ...] = ()
    restrictions: tuple[dict[str, Any], ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def is_active(account: PublishingAccount | None) -> bool:
    return account is not None and account.status != "disconnected"


# ---------------------------------------------------------------------------
# Legacy single-channel YouTube migration
# ---------------------------------------------------------------------------


def migrate_legacy_youtube(db: Session) -> list[PublishingAccount]:
    """Adopt the pre-multi-account ``youtube_connections`` row(s), idempotently.

    Non-destructive: the legacy row is left in place, uploads keep their
    ``channel_id`` (which now resolves to this account) and the legacy
    keyring refresh token is copied to the account's own entry on first use
    (see ``youtube.connection``).  A channel that already has an account is
    never touched again, so a later disconnect is never undone by this.
    """
    legacy_rows = db.scalars(select(YouTubeConnection)).all()
    if not legacy_rows:
        return []
    created: list[PublishingAccount] = []
    for legacy in legacy_rows:
        if not legacy.channel_id:
            continue
        if find_account(db, "youtube", legacy.channel_id, migrate=False) is not None:
            continue
        account = PublishingAccount(
            platform="youtube",
            external_account_id=legacy.channel_id,
            display_name=(legacy.channel_title or "")[:200],
            status=legacy.status or "connected",
            granted_scopes=list(legacy.granted_scopes or []),
            last_error_code=legacy.last_error_code,
            last_error_message=legacy.last_error_message,
            connected_at=legacy.connected_at or _now(),
            details={
                "migrated_from": "youtube_connections",
                "legacy_slot": legacy.slot,
                # Only the old "primary" channel owned the old global token.
                "legacy_secret": legacy.slot == LEGACY_YOUTUBE_SLOT,
            },
        )
        db.add(account)
        created.append(account)
    if not created:
        return []
    try:
        db.flush()
        has_default = db.scalar(
            select(PublishingAccount.id).where(PublishingAccount.platform == "youtube", PublishingAccount.is_default.is_(True))
        )
        if has_default is None:
            primary = next((item for item in created if item.details.get("legacy_slot") == LEGACY_YOUTUBE_SLOT and is_active(item)), None)
            if primary is not None:
                primary.is_default = True
        db.commit()
    except IntegrityError:
        db.rollback()  # a concurrent request migrated it first
        return []
    return created


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _migrate(db: Session, platform: str | None) -> None:
    if platform in (None, "youtube"):
        migrate_legacy_youtube(db)


def list_accounts(db: Session, platform: str | None = None, *, include_disconnected: bool = False) -> list[PublishingAccount]:
    _migrate(db, platform)
    query = select(PublishingAccount)
    if platform:
        query = query.where(PublishingAccount.platform == platform)
    if not include_disconnected:
        query = query.where(PublishingAccount.status != "disconnected")
    rows = db.scalars(query).all()
    order = {name: index for index, name in enumerate(PLATFORMS)}
    return sorted(rows, key=lambda item: (order.get(item.platform, 9), not item.is_default, aware(item.connected_at) or _now(), item.id))


def get_account(db: Session, account_id: str | None) -> PublishingAccount | None:
    if not account_id:
        return None
    _migrate(db, None)
    return db.get(PublishingAccount, account_id)


def find_account(db: Session, platform: str, external_account_id: str, *, migrate: bool = True) -> PublishingAccount | None:
    if migrate:
        _migrate(db, platform)
    return db.scalar(
        select(PublishingAccount).where(
            PublishingAccount.platform == platform,
            PublishingAccount.external_account_id == external_account_id,
        )
    )


def default_account(db: Session, platform: str, *, migrate: bool = True) -> PublishingAccount | None:
    """The platform's default (only an initial selection), else its oldest active account."""
    if migrate:
        _migrate(db, platform)
    rows = db.scalars(
        select(PublishingAccount).where(PublishingAccount.platform == platform, PublishingAccount.status != "disconnected")
    ).all()
    if not rows:
        return None
    marked = next((item for item in rows if item.is_default), None)
    return marked or min(rows, key=lambda item: (aware(item.connected_at) or _now(), item.id))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def upsert_identity(db: Session, identity: AccountIdentity) -> tuple[PublishingAccount, bool]:
    """Connect (or reconnect) one account; returns ``(account, created)``.

    There is deliberately no account cap: provider limits still apply.
    """
    now = _now()
    account = find_account(db, identity.platform, identity.external_account_id)
    created = account is None
    if account is None:
        account = PublishingAccount(platform=identity.platform, external_account_id=identity.external_account_id, connected_at=now)
        db.add(account)
    account.display_name = (identity.display_name or identity.handle or identity.external_account_id)[:200]
    account.handle = identity.handle[:200] if identity.handle else None
    account.avatar_url = identity.avatar_url or None
    account.granted_scopes = list(identity.granted_scopes)
    account.restrictions = [dict(item) for item in identity.restrictions]
    account.details = {**(account.details or {}), **identity.details}
    account.status = "connected"
    account.last_error_code = None
    account.last_error_message = None
    account.connected_at = now
    account.disconnected_at = None
    account.updated_at = now
    db.flush()
    has_default = db.scalar(
        select(PublishingAccount.id).where(
            PublishingAccount.platform == identity.platform,
            PublishingAccount.is_default.is_(True),
            PublishingAccount.status != "disconnected",
        )
    )
    if has_default is None:
        account.is_default = True  # the first connected account is the initial selection
    db.commit()
    db.refresh(account)
    return account, created


def set_default(db: Session, account: PublishingAccount) -> PublishingAccount:
    if not is_active(account):
        raise ValueError("Only a connected account can be the default.")
    for other in db.scalars(select(PublishingAccount).where(PublishingAccount.platform == account.platform, PublishingAccount.is_default.is_(True))).all():
        other.is_default = False
    db.flush()
    account.is_default = True
    db.commit()
    db.refresh(account)
    return account


def set_error(db: Session, account: PublishingAccount, code: str | None, message: str | None, *, status: str | None = None) -> None:
    account.last_error_code = code
    account.last_error_message = message[:500] if message else None
    if status:
        account.status = status
    account.updated_at = _now()
    db.commit()


def mark_disconnected(db: Session, account: PublishingAccount) -> PublishingAccount:
    account.status = "disconnected"
    account.is_default = False
    account.last_error_code = None
    account.last_error_message = None
    account.disconnected_at = _now()
    account.updated_at = _now()
    db.commit()
    return account


# ---------------------------------------------------------------------------
# Non-secret platform configuration
# ---------------------------------------------------------------------------

PLATFORM_CONFIG_DEFAULTS: dict[str, dict[str, Any]] = {
    # Unaudited TikTok clients may only post SELF_ONLY to private accounts.
    "tiktok": {"app_audited": False},
    # Optional Facebook Login for Business configuration id.
    "instagram": {"login_config_id": None},
    "youtube": {},
}


def platform_config(db: Session, platform: str) -> dict[str, Any]:
    row = db.get(PublishingPlatformConfig, platform)
    return {**PLATFORM_CONFIG_DEFAULTS.get(platform, {}), **((row.values if row else None) or {})}


def save_platform_config(db: Session, platform: str, values: dict[str, Any]) -> dict[str, Any]:
    allowed = PLATFORM_CONFIG_DEFAULTS.get(platform, {})
    clean = {key: value for key, value in values.items() if key in allowed}
    row = db.get(PublishingPlatformConfig, platform)
    if row is None:
        row = PublishingPlatformConfig(platform=platform, values={})
        db.add(row)
    row.values = {**(row.values or {}), **clean}
    db.commit()
    return platform_config(db, platform)


# ---------------------------------------------------------------------------
# Serialization (never credentials)
# ---------------------------------------------------------------------------


def serialize_account(account: PublishingAccount, capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
    details = account.details or {}
    return {
        "id": account.id,
        "platform": account.platform,
        "platform_label": PLATFORM_LABELS.get(account.platform, account.platform),
        "external_account_id": account.external_account_id,
        "display_name": account.display_name,
        "handle": account.handle,
        "avatar_url": account.avatar_url,
        "status": account.status,
        "is_default": bool(account.is_default),
        "granted_scopes": list(account.granted_scopes or []),
        "capabilities": capabilities or {},
        "restrictions": list(account.restrictions or []),
        "error": {"code": account.last_error_code, "message": account.last_error_message} if account.last_error_code else None,
        "connected_at": aware(account.connected_at),
        "updated_at": aware(account.updated_at),
        "disconnected_at": aware(account.disconnected_at),
        "token_expires_at": details.get("token_expires_at"),
        "profile_url": details.get("profile_url"),
    }


def label(account: PublishingAccount) -> str:
    name = f"@{account.handle.lstrip('@')}" if account.handle and account.platform != "youtube" else None
    return f"{PLATFORM_LABELS.get(account.platform, account.platform)} · {name or account.display_name or account.external_account_id}"

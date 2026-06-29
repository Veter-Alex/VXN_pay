from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import UserAccountLink
from app.schemas.user import ConnectionInfo
from app.services.marzban import MarzbanError, marzban_client


def _expire_to_datetime(expire_ts: int | None) -> datetime | None:
    if not expire_ts:
        return None
    return datetime.fromtimestamp(expire_ts, tz=UTC)


def apply_panel_user_to_link(link: UserAccountLink, panel_user: dict[str, Any]) -> None:
    link.status_cache = panel_user.get("status")
    link.expires_at_cache = _expire_to_datetime(panel_user.get("expire"))
    link.data_limit_cache = panel_user.get("data_limit")
    link.data_used_cache = panel_user.get("used_traffic")
    link.last_synced_at = datetime.now(UTC)


def build_connection_info(link: UserAccountLink) -> ConnectionInfo:
    status = link.status_cache or "pending_sync"
    return ConnectionInfo(
        name=link.account_username,
        status=status,
        expires_at=link.expires_at_cache,
        data_limit_bytes=link.data_limit_cache,
        data_used_bytes=link.data_used_cache,
    )


async def sync_account_link(db: AsyncSession, link: UserAccountLink) -> dict[str, Any]:
    panel_user = await marzban_client.get_user(link.account_username)
    apply_panel_user_to_link(link, panel_user)
    await db.flush()
    return panel_user


async def extend_account_link(db: AsyncSession, link: UserAccountLink, period_days: int) -> int:
    new_expire = await marzban_client.extend_expire(link.account_username, period_days)
    panel_user = await marzban_client.get_user(link.account_username)
    apply_panel_user_to_link(link, panel_user)
    await db.flush()
    return new_expire

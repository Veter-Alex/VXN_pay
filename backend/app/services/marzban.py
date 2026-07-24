import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class MarzbanError(Exception):
    """Ошибка при обращении к Marzban API."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MarzbanClient:
    """HTTP-клиент Marzban с кэшем admin JWT."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def _base_url(self) -> str:
        return settings.marzban_base_url.rstrip("/")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        headers: dict[str, str] = {}
        if auth:
            token = await self._get_token()
            headers["Authorization"] = f"Bearer {token}"

        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=settings.marzban_request_timeout_seconds) as client:
                response = await client.request(method, url, headers=headers, json=json)
        except httpx.HTTPError as exc:
            raise MarzbanError(f"Панель недоступна: {exc}") from exc

        if response.status_code >= 400:
            raise MarzbanError(
                f"Panel API error {response.status_code}: {response.text}",
                status_code=response.status_code,
            )

        if not response.content:
            return None
        return response.json()

    async def _get_token(self) -> str:
        async with self._lock:
            now = datetime.now(UTC)
            if self._token and self._token_expires_at and now < self._token_expires_at:
                return self._token

            if not settings.marzban_admin_password:
                raise MarzbanError("MARZBAN_ADMIN_PASSWORD не задан")

            try:
                async with httpx.AsyncClient(timeout=settings.marzban_request_timeout_seconds) as client:
                    response = await client.post(
                        f"{self._base_url}/api/admin/token",
                        data={
                            "username": settings.marzban_admin_user,
                            "password": settings.marzban_admin_password,
                        },
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
            except httpx.HTTPError as exc:
                raise MarzbanError(f"Панель недоступна: {exc}") from exc

            if response.status_code >= 400:
                raise MarzbanError(
                    f"Не удалось получить токен панели: {response.status_code}",
                    status_code=response.status_code,
                )

            data = response.json()
            self._token = data["access_token"]
            self._token_expires_at = now + timedelta(minutes=settings.marzban_token_cache_minutes)
            return self._token

    async def health_check(self) -> dict[str, Any]:
        token = await self._get_token()
        return {"reachable": True, "token_obtained": bool(token)}

    async def get_user(self, username: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/user/{username}")

    async def list_users_page(self, *, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """Возвращает одну страницу списка VPN-пользователей Marzban."""
        return await self._request("GET", f"/api/users?offset={offset}&limit={limit}")

    async def list_all_users(self) -> list[dict[str, Any]]:
        """Загружает всех VPN-пользователей Marzban (с пагинацией)."""
        users: list[dict[str, Any]] = []
        offset = 0
        limit = 100
        while True:
            page = await self.list_users_page(offset=offset, limit=limit)
            batch = page.get("users") or []
            users.extend(batch)
            total = int(page.get("total") or len(users))
            offset += limit
            if offset >= total or not batch:
                break
        return users

    async def create_user(
        self,
        username: str,
        *,
        expire: int = 0,
        status: str = "active",
        note: str | None = None,
    ) -> dict[str, Any]:
        """Создаёт VPN-пользователя в Marzban с inbound из конфигурации."""
        inbound_tag = settings.marzban_inbound_tag
        payload: dict[str, Any] = {
            "username": username,
            "proxies": {"vless": {"flow": ""}},
            "inbounds": {"vless": [inbound_tag]},
            "expire": expire,
            "data_limit": 0,
            "data_limit_reset_strategy": "no_reset",
            "status": status,
        }
        if note:
            payload["note"] = note
        return await self._request("POST", "/api/user", json=payload)

    async def modify_user(self, username: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", f"/api/user/{username}", json=payload)

    async def delete_user(self, username: str) -> None:
        await self._request("DELETE", f"/api/user/{username}")

    async def extend_expire(self, username: str, period_days: int) -> int:
        user = await self.get_user(username)
        now_ts = int(datetime.now(UTC).timestamp())
        current_expire = int(user.get("expire") or 0)
        base = max(current_expire, now_ts)
        new_expire = base + period_days * 86400

        await self.modify_user(username, {"expire": new_expire, "status": "active"})
        logger.info("Extended %s until %s", username, new_expire)
        return new_expire

    def extract_subscription_url(self, panel_user: dict[str, Any]) -> str | None:
        """Извлекает subscription URL из ответа Marzban."""
        if panel_user.get("subscription_url"):
            return panel_user["subscription_url"]
        links = panel_user.get("links") or panel_user.get("subscription_links")
        if isinstance(links, list) and links:
            return links[0]
        if isinstance(links, str):
            return links
        return None


marzban_client = MarzbanClient()

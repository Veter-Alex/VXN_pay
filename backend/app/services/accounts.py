"""Управление учётными записями сайта и VPN-пользователями Marzban."""

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import hash_password
from app.models.user import (
    InviteToken,
    ManagerAssignment,
    TariffCategory,
    User,
    UserAccountLink,
    UserRole,
)
from app.services.acl import can_manage_user
from app.services.connections import apply_panel_user_to_link, sync_account_link
from app.services.marzban import MarzbanError, marzban_client


class AccountError(Exception):
    """Ошибка бизнес-логики управления аккаунтами."""


# Правила Marzban: 3–32 символа, a-z / 0-9, подчёркивание только внутри
_USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_]{0,30}[a-z0-9])?$")
USERNAME_RULES_HINT = (
    "Логин: 3–32 символа, только латиница a–z, цифры и подчёркивание "
    "(не в начале и не в конце)"
)


def normalize_vpn_username(username: str) -> str:
    """Нормализует логин: обрезка пробелов и приведение к нижнему регистру."""
    return username.strip().lower()


def validate_vpn_username(username: str) -> str:
    """
    Проверяет логин по правилам Marzban до обращения к панели.

    Возвращает нормализованный логин.
    Raises:
        AccountError: если логин некорректен.
    """
    cleaned = normalize_vpn_username(username)
    if len(cleaned) < 3 or len(cleaned) > 32:
        raise AccountError(USERNAME_RULES_HINT)
    if not _USERNAME_RE.fullmatch(cleaned):
        raise AccountError(USERNAME_RULES_HINT)
    return cleaned


def _random_password_placeholder() -> str:
    """Генерирует временный пароль для invite-flow (не сохраняется в открытом виде)."""
    return secrets.token_urlsafe(24)


async def create_vpn_account(
    db: AsyncSession,
    *,
    actor: User,
    username: str,
    email: str | None = None,
    tariff_category: TariffCategory,
    comments: str | None = None,
    role: UserRole = UserRole.user,
    expire_ts: int | None = None,
    send_invite: bool = True,
) -> tuple[User, str | None]:
    """
    Создаёт VPN-пользователя в Marzban и учётную запись на сайте.

    Почта необязательна: если не указана, остаётся пустой —
    пользователь задаст её по invite-ссылке.

    Возвращает (user, raw_invite_token или None).
    """
    if actor.role not in (UserRole.admin, UserRole.manager):
        raise AccountError("Недостаточно прав для создания пользователя")

    username = validate_vpn_username(username)

    existing = await db.execute(select(User).where(User.login == username))
    if existing.scalar_one_or_none() is not None:
        raise AccountError(f"Логин '{username}' уже занят")

    existing_link = await db.execute(
        select(UserAccountLink).where(UserAccountLink.account_username == username)
    )
    if existing_link.scalar_one_or_none() is not None:
        raise AccountError(f"VPN-пользователь '{username}' уже привязан")

    if role in (UserRole.admin, UserRole.manager) and actor.role != UserRole.admin:
        raise AccountError("Только администратор может назначать роли admin/manager")

    try:
        panel_user = await marzban_client.create_user(
            username,
            expire=expire_ts or 0,
            status="active",
            note=comments,
        )
    except MarzbanError as exc:
        raise AccountError(f"Не удалось создать пользователя в Marzban: {exc}") from exc

    email_clean = (email or "").strip() or None

    placeholder = _random_password_placeholder()
    user = User(
        login=username,
        password_hash=hash_password(placeholder),
        email=email_clean,
        role=role,
        tariff_category=tariff_category,
        comments=comments,
        password_set=False,
    )
    db.add(user)
    await db.flush()

    link = UserAccountLink(user_id=user.id, account_username=username)
    apply_panel_user_to_link(link, panel_user)
    link.subscription_url_cache = marzban_client.extract_subscription_url(panel_user)
    db.add(link)
    await db.flush()

    # Менеджер сразу получает нового пользователя в свой список подопечных
    if actor.role == UserRole.manager and user.id != actor.id:
        db.add(ManagerAssignment(manager_user_id=actor.id, managed_user_id=user.id))
        await db.flush()

    raw_invite: str | None = None
    if send_invite:
        raw_invite = await create_invite_token(db, user)

    return user, raw_invite


async def create_invite_token(db: AsyncSession, user: User, hours: int = 72) -> str:
    """Создаёт одноразовый invite-токен и возвращает raw token."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    invite = InviteToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(hours=hours),
    )
    db.add(invite)
    await db.flush()
    return raw_token


async def update_vpn_account(
    db: AsyncSession,
    *,
    actor: User,
    user_id: UUID,
    email: str | None = None,
    comments: str | None = None,
    tariff_category: TariffCategory | None = None,
    is_active: bool | None = None,
    marzban_status: str | None = None,
    expire_date: datetime | None = None,
    new_password: str | None = None,
) -> User:
    """Обновляет учётную запись и синхронизирует изменения с Marzban."""
    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.account_links))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise AccountError("Пользователь не найден")

    if not await can_manage_user(db, actor, user):
        raise AccountError("Недостаточно прав")

    if email is not None:
        cleaned = email.strip() or None
        user.email = cleaned
        user.email_verified_at = None

    if comments is not None:
        user.comments = comments

    if tariff_category is not None and actor.role == UserRole.admin:
        user.tariff_category = tariff_category

    if is_active is not None:
        user.is_active = is_active

    if new_password:
        user.password_hash = hash_password(new_password)
        user.password_set = True

    for link in user.account_links:
        payload: dict = {}
        if marzban_status is not None:
            payload["status"] = marzban_status
        if expire_date is not None:
            payload["expire"] = int(expire_date.timestamp())
        if comments is not None:
            payload["note"] = comments

        if payload:
            try:
                panel_user = await marzban_client.modify_user(link.account_username, payload)
                apply_panel_user_to_link(link, panel_user)
                link.subscription_url_cache = marzban_client.extract_subscription_url(panel_user)
            except MarzbanError as exc:
                raise AccountError(f"Ошибка Marzban: {exc}") from exc
        else:
            try:
                await sync_account_link(db, link)
            except MarzbanError:
                pass

    await db.flush()
    return user


async def clear_user_password(
    db: AsyncSession,
    *,
    actor: User,
    user_id: UUID,
) -> User:
    """
    Сбрасывает пароль пользователя (password_set=False).

    Старый пароль перестаёт действовать. Неиспользованные invite-ссылки
    инвалидируются — нужно скопировать новое приглашение.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise AccountError("Пользователь не найден")

    if not await can_manage_user(db, actor, user):
        raise AccountError("Недостаточно прав")

    if user.role == UserRole.admin and actor.role != UserRole.admin:
        raise AccountError("Нельзя сбросить пароль администратора")

    if not user.password_set:
        raise AccountError("Пароль у пользователя ещё не задан")

    user.password_hash = hash_password(_random_password_placeholder())
    user.password_set = False

    now = datetime.now(UTC)
    await db.execute(
        update(InviteToken)
        .where(InviteToken.user_id == user.id, InviteToken.used_at.is_(None))
        .values(used_at=now)
    )
    await db.flush()
    return user


async def delete_vpn_account(db: AsyncSession, *, actor: User, user_id: UUID) -> None:
    """Удаляет учётную запись сайта и VPN-пользователя в Marzban."""
    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.account_links))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise AccountError("Пользователь не найден")

    if user.role == UserRole.admin:
        raise AccountError("Нельзя удалить администратора")

    if not await can_manage_user(db, actor, user):
        raise AccountError("Недостаточно прав")

    for link in user.account_links:
        try:
            await marzban_client.delete_user(link.account_username)
        except MarzbanError as exc:
            if exc.status_code != 404:
                raise AccountError(f"Не удалось удалить из Marzban: {exc}") from exc

    await db.delete(user)
    await db.flush()


async def set_manager_role(
    db: AsyncSession,
    *,
    actor: User,
    user_id: UUID,
    managed_user_ids: list[UUID],
) -> User:
    """Назначает пользователя менеджером и задаёт список подопечных."""
    if actor.role != UserRole.admin:
        raise AccountError("Только администратор может назначать менеджеров")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise AccountError("Пользователь не найден")

    user.role = UserRole.manager
    await db.execute(delete(ManagerAssignment).where(ManagerAssignment.manager_user_id == user.id))

    for managed_id in managed_user_ids:
        if managed_id == user.id:
            continue
        assignment = ManagerAssignment(manager_user_id=user.id, managed_user_id=managed_id)
        db.add(assignment)

    await db.flush()
    return user


async def sync_user_links(db: AsyncSession, user: User) -> None:
    """Синхронизирует все VPN-привязки пользователя с Marzban."""
    for link in user.account_links:
        try:
            await sync_account_link(db, link)
        except MarzbanError:
            pass


async def sync_user_links_if_stale(
    db: AsyncSession,
    user: User,
    *,
    max_age_seconds: int | None = None,
) -> None:
    """
    Синхронизирует привязки только если кэш устарел.

    Нужен для личного кабинета: не дергать Marzban на каждое обновление страницы.
    max_age_seconds=0 или None из настроек с 0 — пропуск (без обращения к панели).
    """
    from app.config import get_settings

    ttl = get_settings().marzban_cabinet_sync_ttl_seconds if max_age_seconds is None else max_age_seconds
    if ttl <= 0:
        return

    now = datetime.now(UTC)
    for link in user.account_links:
        last = link.last_synced_at
        if last is not None:
            # naive datetime из БД трактуем как UTC
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if (now - last).total_seconds() < ttl:
                continue
        try:
            await sync_account_link(db, link)
        except MarzbanError:
            pass


async def import_users_from_marzban(db: AsyncSession, *, actor: User) -> dict[str, int]:
    """
    Импортирует VPN-пользователей из Marzban в VXN_Pay.

    Для каждого пользователя Marzban без привязки создаёт запись на сайте.
    Уже привязанные аккаунты только обновляет (sync).
    """
    if actor.role != UserRole.admin:
        raise AccountError("Только администратор может импортировать из Marzban")

    try:
        panel_users = await marzban_client.list_all_users()
    except MarzbanError as exc:
        raise AccountError(f"Не удалось получить список из Marzban: {exc}") from exc

    links_result = await db.execute(select(UserAccountLink.account_username))
    linked_usernames = {row[0] for row in links_result.all()}

    logins_result = await db.execute(select(User.login, User.id))
    login_to_user_id = {row[0]: row[1] for row in logins_result.all()}

    imported = 0
    linked = 0
    synced = 0
    skipped = 0

    for summary in panel_users:
        username = summary.get("username")
        if not username:
            skipped += 1
            continue

        if username in linked_usernames:
            link_result = await db.execute(
                select(UserAccountLink).where(UserAccountLink.account_username == username)
            )
            link = link_result.scalar_one_or_none()
            if link is not None:
                try:
                    await sync_account_link(db, link)
                    synced += 1
                except MarzbanError:
                    skipped += 1
            continue

        try:
            panel_user = await marzban_client.get_user(username)
        except MarzbanError:
            skipped += 1
            continue

        note = panel_user.get("note") or summary.get("note")
        placeholder = _random_password_placeholder()

        if username in login_to_user_id:
            user_result = await db.execute(
                select(User)
                .where(User.id == login_to_user_id[username])
                .options(selectinload(User.account_links))
            )
            user = user_result.scalar_one_or_none()
            if user is None:
                skipped += 1
                continue
            link = UserAccountLink(user_id=user.id, account_username=username)
            apply_panel_user_to_link(link, panel_user)
            link.subscription_url_cache = marzban_client.extract_subscription_url(panel_user)
            db.add(link)
            linked_usernames.add(username)
            linked += 1
            await db.flush()
            continue

        user = User(
            login=username,
            password_hash=hash_password(placeholder),
            email=None,
            role=UserRole.user,
            tariff_category=TariffCategory.commercial,
            comments=note,
            password_set=False,
            is_active=panel_user.get("status", "active") != "disabled",
        )
        db.add(user)
        await db.flush()

        link = UserAccountLink(user_id=user.id, account_username=username)
        apply_panel_user_to_link(link, panel_user)
        link.subscription_url_cache = marzban_client.extract_subscription_url(panel_user)
        db.add(link)
        linked_usernames.add(username)
        login_to_user_id[username] = user.id
        imported += 1

    await db.flush()
    return {
        "imported": imported,
        "linked": linked,
        "synced": synced,
        "skipped": skipped,
        "total_marzban": len(panel_users),
    }

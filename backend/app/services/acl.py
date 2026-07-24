"""Проверка прав доступа admin/manager к учётным записям."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import ManagerAssignment, User, UserRole


async def get_managed_user_ids(db: AsyncSession, manager_id: UUID) -> set[UUID]:
    """Возвращает ID подопечных пользователей менеджера."""
    result = await db.execute(
        select(ManagerAssignment.managed_user_id).where(ManagerAssignment.manager_user_id == manager_id)
    )
    return {row[0] for row in result.all()}


async def can_manage_user(db: AsyncSession, actor: User, target: User) -> bool:
    """Проверяет, может ли actor управлять target."""
    if actor.role == UserRole.admin:
        return True
    if actor.role != UserRole.manager:
        return actor.id == target.id

    if actor.id == target.id:
        return True

    managed_ids = await get_managed_user_ids(db, actor.id)
    return target.id in managed_ids


async def list_visible_users(db: AsyncSession, actor: User) -> list[User]:
    """Список пользователей, видимых actor в кабинете управления."""
    if actor.role == UserRole.admin:
        result = await db.execute(select(User).order_by(User.login))
        return list(result.scalars().all())

    if actor.role == UserRole.manager:
        managed_ids = await get_managed_user_ids(db, actor.id)
        visible_ids = managed_ids | {actor.id}
        result = await db.execute(select(User).where(User.id.in_(visible_ids)).order_by(User.login))
        return list(result.scalars().all())

    result = await db.execute(select(User).where(User.id == actor.id))
    user = result.scalar_one_or_none()
    return [user] if user else []


async def list_managers(db: AsyncSession) -> list[User]:
    """Все пользователи с ролью manager."""
    result = await db.execute(select(User).where(User.role == UserRole.manager).order_by(User.login))
    return list(result.scalars().all())


async def list_assignable_users(db: AsyncSession) -> list[User]:
    """Пользователи, которых можно назначить подопечными (не admin/manager)."""
    result = await db.execute(
        select(User)
        .where(User.role == UserRole.user)
        .order_by(User.login)
    )
    return list(result.scalars().all())

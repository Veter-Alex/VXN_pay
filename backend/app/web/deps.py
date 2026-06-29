from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.jwt import decode_access_token
from app.models.user import User

settings = get_settings()


async def get_user_from_cookie(request: Request, db: AsyncSession) -> User | None:
    token = request.cookies.get(settings.cookie_name)
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        user_id = UUID(payload["sub"])
    except (ValueError, KeyError):
        return None

    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.account_links))
    )
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


async def require_user_from_cookie(request: Request, db: AsyncSession) -> User:
    user = await get_user_from_cookie(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user

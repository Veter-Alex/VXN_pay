from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, require_admin
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models.user import User, UserAccountLink
from app.schemas.user import (
    UserCreateRequest,
    UserCreateResponse,
    UserMeResponse,
    UserMeUpdateRequest,
)
from app.services.connections import build_connection_info

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserMeResponse)
async def get_me(current_user: User = Depends(get_current_user)) -> UserMeResponse:
    return UserMeResponse(
        id=current_user.id,
        login=current_user.login,
        email=current_user.email,
        phone=current_user.phone,
        role=current_user.role,
        connections=[build_connection_info(link) for link in current_user.account_links],
        created_at=current_user.created_at,
    )


@router.put("/me", response_model=UserMeResponse)
async def update_me(
    body: UserMeUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserMeResponse:
    if body.new_password is not None:
        if body.old_password is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Для смены пароля укажите текущий пароль",
            )
        if not verify_password(body.old_password, current_user.password_hash):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неверный текущий пароль")
        current_user.password_hash = hash_password(body.new_password)

    if body.email is not None:
        current_user.email = body.email

    if body.phone is not None:
        current_user.phone = body.phone

    await db.flush()
    await db.refresh(current_user)

    return UserMeResponse(
        id=current_user.id,
        login=current_user.login,
        email=current_user.email,
        phone=current_user.phone,
        role=current_user.role,
        connections=[build_connection_info(link) for link in current_user.account_links],
        created_at=current_user.created_at,
    )


@router.post("", response_model=UserCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreateRequest,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UserCreateResponse:
    existing_login = await db.execute(select(User).where(User.login == body.login))
    if existing_login.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Логин уже занят")

    for username in body.account_usernames:
        existing_link = await db.execute(
            select(UserAccountLink).where(UserAccountLink.account_username == username)
        )
        if existing_link.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Учётная запись '{username}' уже привязана",
            )

    user = User(
        login=body.login,
        password_hash=hash_password(body.password),
        email=body.email,
        phone=body.phone,
        role=body.role,
        comments=body.comments,
    )
    db.add(user)
    await db.flush()

    for username in body.account_usernames:
        db.add(UserAccountLink(user_id=user.id, account_username=username))

    await db.flush()
    await db.refresh(user)

    return UserCreateResponse(
        id=user.id,
        login=user.login,
        email=user.email,
        phone=user.phone,
        role=user.role,
        account_usernames=[link.account_username for link in user.account_links],
        comments=user.comments,
        created_at=user.created_at,
    )

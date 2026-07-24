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
        tariff_category=current_user.tariff_category,
        email_verified_at=current_user.email_verified_at,
        password_set=current_user.password_set,
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
        current_user.password_set = True

    if body.email is not None:
        current_user.email = body.email
        current_user.email_verified_at = None

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
        tariff_category=current_user.tariff_category,
        email_verified_at=current_user.email_verified_at,
        password_set=current_user.password_set,
        connections=[build_connection_info(link) for link in current_user.account_links],
        created_at=current_user.created_at,
    )


@router.post("", response_model=UserCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreateRequest,
    actor: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UserCreateResponse:
    from app.services.accounts import AccountError, create_vpn_account

    try:
        user, _ = await create_vpn_account(
            db,
            actor=actor,
            username=body.login,
            email=body.email,
            tariff_category=body.tariff_category,
            comments=body.comments,
            role=body.role,
            send_invite=False,
        )
        if body.password:
            user.password_hash = hash_password(body.password)
            user.password_set = True
        await db.commit()
        await db.refresh(user)
    except AccountError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return UserCreateResponse(
        id=user.id,
        login=user.login,
        email=user.email,
        phone=user.phone,
        role=user.role,
        tariff_category=user.tariff_category,
        account_usernames=[link.account_username for link in user.account_links],
        comments=user.comments,
        created_at=user.created_at,
    )

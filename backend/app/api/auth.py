import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.jwt import create_access_token
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models.user import PasswordResetToken, User
from app.schemas.auth import ForgotPasswordRequest, MessageResponse, ResetPasswordRequest, TokenResponse

logger = logging.getLogger(__name__)
settings = get_settings()
limiter = Limiter(key_func=get_remote_address)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
@limiter.limit(settings.login_rate_limit)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    result = await db.execute(select(User).where(User.login == form_data.username))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Учётная запись деактивирована")

    token = create_access_token(user.id, user.role.value)
    return TokenResponse(access_token=token)


@router.post("/forgot-password", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    body: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if user is None:
        return MessageResponse(message="Если email зарегистрирован, инструкции будут отправлены")

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    reset_token = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(reset_token)
    await db.flush()

    if settings.smtp_host:
        logger.info("SMTP not implemented yet; reset token for user %s saved", user.login)
    else:
        logger.info(
            "Password reset requested for %s (SMTP not configured, token saved in DB only)",
            user.login,
        )

    return MessageResponse(message="Если email зарегистрирован, инструкции будут отправлены")


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    now = datetime.now(UTC)

    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
    )
    reset_token = result.scalar_one_or_none()

    if reset_token is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Недействительный или просроченный токен")

    user_result = await db.execute(select(User).where(User.id == reset_token.user_id))
    user = user_result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Пользователь не найден")

    user.password_hash = hash_password(body.new_password)
    reset_token.used_at = now

    return MessageResponse(message="Пароль успешно изменён")

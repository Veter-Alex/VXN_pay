from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.jwt import create_access_token
from app.core.security import verify_password
from app.db.session import get_db
from app.models.payment import Payment, PaymentStatus
from app.models.tariff import Tariff
from app.models.user import User
from app.services.billing import BillingError, create_payment_for_user, process_payment_succeeded
from app.services.connections import build_connection_info, sync_account_link
from app.services.marzban import MarzbanError
from app.services.yukassa import yukassa_service
from app.web.deps import get_user_from_cookie

settings = get_settings()
templates = Jinja2Templates(directory="app/templates")
router = APIRouter(tags=["site"])


def _format_expires(dt) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y")


def _landing_context(user: User | None, **extra) -> dict:
    return {
        "user": user,
        "login_error": None,
        "login_value": "",
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_user_from_cookie(request, db)
    ctx = _landing_context(user)
    return templates.TemplateResponse(request, "index.html", ctx)


@router.get("/login")
async def login_page():
    return RedirectResponse(url="/#login", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/login")
async def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.login == login))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash) or not user.is_active:
        ctx = _landing_context(
            None,
            login_error="Неверный логин или пароль",
            login_value=login,
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            ctx,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    token = create_access_token(user.id, user.role.value)
    response = RedirectResponse(url="/cabinet", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.jwt_access_token_expire_minutes * 60,
    )
    return response


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(
        request,
        "forgot_password.html",
        {"user": None, "message": "Восстановление пароля будет доступно после настройки почты."},
    )


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(settings.cookie_name)
    return response


@router.get("/cabinet", response_class=HTMLResponse)
async def cabinet(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_user_from_cookie(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    for link in user.account_links:
        try:
            await sync_account_link(db, link)
        except MarzbanError:
            pass

    tariffs_result = await db.execute(
        select(Tariff).where(Tariff.is_active.is_(True)).order_by(Tariff.period_days)
    )
    tariffs = list(tariffs_result.scalars().all())

    connections = [build_connection_info(link) for link in user.account_links]

    return templates.TemplateResponse(
        request,
        "cabinet.html",
        {
            "user": user,
            "connections": connections,
            "tariffs": tariffs,
            "format_expires": _format_expires,
            "stub_mode": yukassa_service.is_stub,
        },
    )


@router.post("/pay/create")
async def pay_create(
    request: Request,
    tariff_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_user_from_cookie(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    result = await db.execute(select(User).where(User.id == user.id).options(selectinload(User.account_links)))
    user = result.scalar_one_or_none()
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        payment, confirmation_url = await create_payment_for_user(db, user, tariff_id)
        await db.refresh(payment, attribute_names=["tariff"])
    except BillingError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return RedirectResponse(url=confirmation_url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/pay/stub/{payment_id}", response_class=HTMLResponse)
async def pay_stub_page(payment_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_user_from_cookie(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    result = await db.execute(
        select(Payment)
        .where(Payment.id == payment_id, Payment.user_id == user.id)
        .options(selectinload(Payment.tariff))
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Платёж не найден")

    return templates.TemplateResponse(
        request,
        "pay_stub.html",
        {"payment": payment, "stub_mode": yukassa_service.is_stub, "user": user},
    )


@router.post("/pay/stub/{payment_id}/confirm")
async def pay_stub_confirm(payment_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_user_from_cookie(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    result = await db.execute(
        select(Payment).where(Payment.id == payment_id, Payment.user_id == user.id)
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Платёж не найден")
    if payment.status != PaymentStatus.pending:
        return RedirectResponse(url="/pay/success", status_code=status.HTTP_303_SEE_OTHER)

    try:
        await process_payment_succeeded(
            db,
            yukassa_payment_id=payment.yukassa_payment_id,
            amount=payment.amount,
        )
    except BillingError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return RedirectResponse(url="/pay/success", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/pay/success", response_class=HTMLResponse)
async def pay_success(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "success.html", {"user": user})

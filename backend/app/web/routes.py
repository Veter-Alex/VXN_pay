"""HTML-роуты сайта: главная, кабинеты, invite, docs."""

import hashlib
from datetime import UTC, datetime
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import create_email_verification_token, verify_email_token
from app.config import get_settings
from app.core.jwt import create_access_token
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models.payment import Payment, PaymentStatus
from app.models.tariff import Tariff
from app.models.user import InviteToken, TariffCategory, User, UserRole
from app.services.accounts import (
    AccountError,
    clear_user_password,
    create_invite_token,
    create_vpn_account,
    delete_vpn_account,
    import_users_from_marzban,
    set_manager_role,
    sync_user_links_if_stale,
    update_vpn_account,
)
from app.services.acl import (
    get_managed_user_ids,
    list_assignable_users,
    list_managers,
    list_visible_users,
)
from app.services.billing import BillingError, create_payment_for_user, process_payment_succeeded
from app.services.email import email_service
from app.services.yukassa import yukassa_service
from app.web.deps import get_user_from_cookie, require_role_from_cookie, require_user_from_cookie
from app.web.helpers import (
    build_account_row,
    category_label,
    format_expires,
    format_expires_input,
    role_home_url,
)

settings = get_settings()
templates = Jinja2Templates(directory="app/templates")
router = APIRouter(tags=["site"])


def _landing_context(user: User | None, **extra) -> dict:
    return {"user": user, "login_error": None, "login_value": "", **extra}


def _cabinet_nav(user: User) -> dict:
    return {
        "user": user,
        "format_expires": format_expires,
        "format_expires_input": format_expires_input,
        "category_label": category_label,
        "stub_mode": yukassa_service.is_stub,
    }


async def _load_tariffs(db: AsyncSession, category: TariffCategory) -> list[Tariff]:
    result = await db.execute(
        select(Tariff)
        .where(Tariff.is_active.is_(True), Tariff.category == category)
        .order_by(Tariff.period_days)
    )
    return list(result.scalars().all())


async def _load_all_tariffs(db: AsyncSession) -> dict[str, list[Tariff]]:
    pref = await _load_tariffs(db, TariffCategory.preferential)
    comm = await _load_tariffs(db, TariffCategory.commercial)
    return {"preferential": pref, "commercial": comm}


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_user_from_cookie(request, db)
    if user:
        return RedirectResponse(url=role_home_url(user.role), status_code=status.HTTP_303_SEE_OTHER)
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
    if (
        user is None
        or not verify_password(password, user.password_hash)
        or not user.is_active
        or not user.password_set
    ):
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
    response = RedirectResponse(url=role_home_url(user.role), status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.jwt_access_token_expire_minutes * 60,
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(settings.cookie_name)
    return response


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(
        request,
        "forgot_password.html",
        {"user": None, "message": None, "error": None},
    )


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    from app.api.auth import forgot_password as api_forgot_password
    from app.schemas.auth import ForgotPasswordRequest

    await api_forgot_password(ForgotPasswordRequest(email=email), db)
    return templates.TemplateResponse(
        request,
        "forgot_password.html",
        {
            "user": None,
            "message": "Если email зарегистрирован, инструкции будут отправлены.",
            "error": None,
        },
    )


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str = ""):
    return templates.TemplateResponse(
        request,
        "reset_password.html",
        {"user": None, "token": token, "message": None, "error": None},
    )


@router.post("/reset-password", response_class=HTMLResponse)
async def reset_password_submit(
    request: Request,
    token: str = Form(...),
    new_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    from app.api.auth import reset_password as api_reset
    from app.schemas.auth import ResetPasswordRequest

    try:
        await api_reset(body=ResetPasswordRequest(token=token, new_password=new_password), db=db)
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"user": None, "token": "", "message": "Пароль успешно изменён. Можно войти.", "error": None},
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"user": None, "token": token, "message": None, "error": exc.detail},
        )


async def _load_invite_user(
    db: AsyncSession, raw_token: str
) -> tuple[InviteToken | None, User | None, str | None, str | None]:
    """
    Проверяет invite-токен и возвращает (invite, user, error, error_kind).

    error_kind: used | expired | invalid | missing_user | None
    """
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(UTC)
    result = await db.execute(select(InviteToken).where(InviteToken.token_hash == token_hash))
    invite = result.scalar_one_or_none()
    if invite is None:
        return None, None, "Ссылка приглашения недействительна.", "invalid"

    user_result = await db.execute(select(User).where(User.id == invite.user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        return invite, None, "Пользователь не найден.", "missing_user"

    # Пароль уже задан — форму не показываем, даже если токен формально не использован
    if user.password_set or invite.used_at is not None:
        return (
            invite,
            user,
            "Пароль по этой ссылке уже был задан ранее. Ссылка больше не действует.",
            "used",
        )

    if invite.expires_at <= now:
        return invite, user, "Срок действия ссылки истёк. Запросите новое приглашение.", "expired"

    return invite, user, None, None


def _invite_email_prefill(email: str | None) -> str:
    """Подставляет почту в форму invite только если она уже задана."""
    return (email or "").strip()


def _invite_page_context(
    *,
    raw_token: str,
    user: User | None,
    error: str | None = None,
    error_kind: str | None = None,
    message: str | None = None,
    invite_email: str = "",
    show_form: bool = False,
) -> dict:
    """Собирает контекст шаблона страницы приглашения."""
    return {
        "user": None,
        "token": raw_token if show_form else "",
        "invite_login": user.login if user else "",
        "invite_email": invite_email,
        "message": message,
        "error": error,
        "error_kind": error_kind,
    }


@router.get("/invite/{raw_token}", response_class=HTMLResponse)
async def invite_page(request: Request, raw_token: str, db: AsyncSession = Depends(get_db)):
    """Страница принятия приглашения: задать пароль и почту восстановления."""
    _invite, user, error, error_kind = await _load_invite_user(db, raw_token)
    show_form = error is None and user is not None
    return templates.TemplateResponse(
        request,
        "invite.html",
        _invite_page_context(
            raw_token=raw_token,
            user=user,
            error=error,
            error_kind=error_kind,
            invite_email=_invite_email_prefill(user.email if user else None),
            show_form=show_form,
        ),
    )


@router.post("/invite/{raw_token}", response_class=HTMLResponse)
async def invite_submit(
    request: Request,
    raw_token: str,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Сохраняет пароль и почту восстановления по invite-ссылке."""
    invite, user, error, error_kind = await _load_invite_user(db, raw_token)

    def render(*, err: str | None = None, kind: str | None = None, msg: str | None = None, show_form: bool = False):
        return templates.TemplateResponse(
            request,
            "invite.html",
            _invite_page_context(
                raw_token=raw_token,
                user=user,
                error=err,
                error_kind=kind,
                message=msg,
                invite_email=email.strip() if email else _invite_email_prefill(user.email if user else None),
                show_form=show_form,
            ),
        )

    if error or invite is None or user is None:
        return render(err=error or "Ссылка приглашения недействительна.", kind=error_kind or "invalid")

    # Повторная защита на случай гонки запросов
    if user.password_set or invite.used_at is not None:
        return render(
            err="Пароль по этой ссылке уже был задан ранее. Ссылка больше не действует.",
            kind="used",
        )

    email_clean = email.strip()
    if not email_clean or "@" not in email_clean:
        return render(err="Укажите корректную почту для восстановления", show_form=True)

    if len(new_password) < 8:
        return render(err="Пароль должен быть не короче 8 символов", show_form=True)

    if new_password != confirm_password:
        return render(err="Пароли не совпадают", show_form=True)

    user.password_hash = hash_password(new_password)
    user.password_set = True
    if user.email != email_clean:
        user.email = email_clean
        user.email_verified_at = None
    invite.used_at = datetime.now(UTC)
    await db.commit()

    return render(msg="Пароль задан. Теперь можно войти.")


@router.get("/verify-email", response_class=HTMLResponse)
async def verify_email_page(request: Request, token: str = "", db: AsyncSession = Depends(get_db)):
    message = None
    error = None
    if token:
        try:
            await verify_email_token(db, token)
            await db.commit()
            message = "Почта успешно подтверждена."
        except HTTPException as exc:
            error = exc.detail
    return templates.TemplateResponse(
        request,
        "verify_email.html",
        {"user": None, "message": message, "error": error},
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, db: AsyncSession = Depends(get_db)):
    """Админ-кабинет: данные из кэша БД, без обхода Marzban на каждый refresh."""
    user = await require_role_from_cookie(request, db, UserRole.admin)
    users = await list_visible_users(db, user)

    rows = [build_account_row(u) for u in users if u.role != UserRole.admin]
    managers = await list_managers(db)
    assignable_users = await list_assignable_users(db)
    # Кандидаты в менеджеры: уже менеджеры (редактирование) + обычные пользователи
    manager_candidates = sorted([*managers, *assignable_users], key=lambda u: u.login.lower())
    manager_assignments = {
        str(m.id): sorted(str(uid) for uid in await get_managed_user_ids(db, m.id))
        for m in managers
    }
    ctx = {
        **_cabinet_nav(user),
        "page_title": "Администрирование",
        "rows": rows,
        "managers": managers,
        "assignable_users": assignable_users,
        "manager_candidates": manager_candidates,
        "manager_assignments": manager_assignments,
        "tariffs_by_category": await _load_all_tariffs(db),
        "message": request.query_params.get("msg"),
        "error": request.query_params.get("err"),
        "is_admin": True,
    }
    return templates.TemplateResponse(request, "manage_panel.html", ctx)


@router.get("/manager", response_class=HTMLResponse)
async def manager_panel(request: Request, db: AsyncSession = Depends(get_db)):
    """Кабинет менеджера: данные из кэша БД, без live-sync всех подопечных."""
    user = await require_role_from_cookie(request, db, UserRole.manager)
    users = await list_visible_users(db, user)

    rows = [build_account_row(u) for u in users]
    ctx = {
        **_cabinet_nav(user),
        "page_title": "Кабинет менеджера",
        "rows": rows,
        "managers": [],
        "assignable_users": [],
        "manager_candidates": [],
        "manager_assignments": {},
        "tariffs_by_category": await _load_all_tariffs(db),
        "message": request.query_params.get("msg"),
        "error": request.query_params.get("err"),
        "is_admin": False,
    }
    return templates.TemplateResponse(request, "manage_panel.html", ctx)


@router.post("/admin/import-marzban")
async def admin_import_marzban(request: Request, db: AsyncSession = Depends(get_db)):
    """Импорт VPN-пользователей из Marzban (только admin)."""
    actor = await require_role_from_cookie(request, db, UserRole.admin)
    try:
        stats = await import_users_from_marzban(db, actor=actor)
        await db.commit()
        msg = (
            f"Marzban: {stats['total_marzban']} всего, "
            f"импортировано {stats['imported']}, "
            f"привязано {stats['linked']}, "
            f"обновлено {stats['synced']}, "
            f"пропущено {stats['skipped']}"
        )
        return RedirectResponse(url=f"/admin?msg={quote(msg)}", status_code=303)
    except AccountError as exc:
        return RedirectResponse(url=f"/admin?err={exc}", status_code=303)


@router.post("/admin/create")
async def admin_create_user(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    tariff_category: str = Form("commercial"),
    comments: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Создаёт пользователя: обязательны логин и категория, почта — опционально."""
    actor = await require_role_from_cookie(request, db, UserRole.admin, UserRole.manager)
    category = TariffCategory(tariff_category)
    panel = "/admin" if actor.role == UserRole.admin else "/manager"
    try:
        new_user, raw_invite = await create_vpn_account(
            db,
            actor=actor,
            username=username.strip(),
            email=email.strip() or None,
            tariff_category=category,
            comments=comments.strip() or None,
        )
        # Письмо шлём только на реальную почту; иначе invite копируют вручную
        invite_url = f"{settings.site_base_url.rstrip('/')}/invite/{raw_invite}" if raw_invite else None
        if invite_url and new_user.email:
            email_service.send_invite(new_user.email, invite_url)
        await db.commit()
        return RedirectResponse(url=f"{panel}?msg=Пользователь+{username}+создан", status_code=303)
    except AccountError as exc:
        return RedirectResponse(url=f"{panel}?err={exc}", status_code=303)


@router.post("/admin/save/{user_id}")
async def admin_save_user(
    request: Request,
    user_id: UUID,
    email: str = Form(""),
    comments: str = Form(""),
    tariff_category: str = Form("commercial"),
    is_active: str = Form(""),
    marzban_active: str = Form(""),
    expire_date: str = Form(""),
    new_password: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    actor = await require_role_from_cookie(request, db, UserRole.admin, UserRole.manager)
    expire_dt = None
    if expire_date.strip():
        expire_dt = datetime.strptime(expire_date.strip(), "%Y-%m-%d").replace(tzinfo=UTC)

    try:
        await update_vpn_account(
            db,
            actor=actor,
            user_id=user_id,
            email=email.strip(),
            comments=comments.strip() or None,
            tariff_category=TariffCategory(tariff_category) if actor.role == UserRole.admin else None,
            is_active=bool(is_active),
            marzban_status="active" if marzban_active else "disabled",
            expire_date=expire_dt,
            new_password=new_password.strip() or None,
        )
        await db.commit()
        panel = "/admin" if actor.role == UserRole.admin else "/manager"
        return RedirectResponse(url=f"{panel}?msg=Сохранено", status_code=303)
    except AccountError as exc:
        panel = "/admin" if actor.role == UserRole.admin else "/manager"
        return RedirectResponse(url=f"{panel}?err={exc}", status_code=303)


@router.post("/admin/delete/{user_id}")
async def admin_delete_user(
    request: Request,
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    actor = await require_role_from_cookie(request, db, UserRole.admin, UserRole.manager)
    try:
        await delete_vpn_account(db, actor=actor, user_id=user_id)
        await db.commit()
        panel = "/admin" if actor.role == UserRole.admin else "/manager"
        return RedirectResponse(url=f"{panel}?msg=Удалено", status_code=303)
    except AccountError as exc:
        panel = "/admin" if actor.role == UserRole.admin else "/manager"
        return RedirectResponse(url=f"{panel}?err={exc}", status_code=303)


@router.post("/admin/clear-password/{user_id}")
async def admin_clear_password(
    request: Request,
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Сбрасывает пароль: пользователь снова задаёт его по новой invite-ссылке."""
    actor = await require_role_from_cookie(request, db, UserRole.admin, UserRole.manager)
    panel = "/admin" if actor.role == UserRole.admin else "/manager"
    try:
        await clear_user_password(db, actor=actor, user_id=user_id)
        await db.commit()
        return RedirectResponse(
            url=f"{panel}?msg=Пароль+сброшен.+Скопируйте+новое+приглашение",
            status_code=303,
        )
    except AccountError as exc:
        return RedirectResponse(url=f"{panel}?err={exc}", status_code=303)


@router.post("/admin/invite/{user_id}")
async def admin_send_invite(
    request: Request,
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Создаёт invite-токен и возвращает ссылку для передачи пользователю.

    При Accept: application/json (кнопка «скопировать приглашение») —
    возвращает JSON с invite_url, письмо не отправляет.
    Иначе — старое поведение: email + редирект.
    """
    actor = await require_role_from_cookie(request, db, UserRole.admin, UserRole.manager)
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    panel = "/admin" if actor.role == UserRole.admin else "/manager"
    wants_json = "application/json" in (request.headers.get("accept") or "")

    if target is None:
        if wants_json:
            return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
        return RedirectResponse(url=f"{panel}?err=Пользователь+не+найден", status_code=303)

    from app.services.acl import can_manage_user

    if not await can_manage_user(db, actor, target):
        if wants_json:
            return JSONResponse({"error": "Недостаточно прав"}, status_code=403)
        return RedirectResponse(url=f"{panel}?err=Недостаточно+прав", status_code=303)

    raw_invite = await create_invite_token(db, target)
    invite_url = f"{settings.site_base_url.rstrip('/')}/invite/{raw_invite}"
    await db.commit()

    if wants_json:
        return JSONResponse({"invite_url": invite_url})

    if target.email:
        email_service.send_invite(target.email, invite_url)
        return RedirectResponse(url=f"{panel}?msg=Invite+отправлен", status_code=303)
    return RedirectResponse(url=f"{panel}?msg=Ссылка+приглашения+создана", status_code=303)


@router.post("/admin/manager")
async def admin_set_manager(
    request: Request,
    manager_id: str = Form(...),
    managed_ids: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
):
    """Назначает выбранного пользователя менеджером и сохраняет список подопечных."""
    actor = await require_role_from_cookie(request, db, UserRole.admin)
    try:
        user_uuid = UUID(manager_id)
        managed_uuids = [UUID(mid) for mid in managed_ids if mid]
        await set_manager_role(db, actor=actor, user_id=user_uuid, managed_user_ids=managed_uuids)
        await db.commit()
        return RedirectResponse(url="/admin?msg=Менеджер+назначен", status_code=303)
    except (AccountError, ValueError) as exc:
        return RedirectResponse(url=f"/admin?err={exc}", status_code=303)


@router.get("/cabinet", response_class=HTMLResponse)
async def cabinet(request: Request, db: AsyncSession = Depends(get_db)):
    user = await require_user_from_cookie(request, db)
    if user.role == UserRole.admin:
        return RedirectResponse(url="/admin", status_code=303)
    if user.role == UserRole.manager:
        return RedirectResponse(url="/manager", status_code=303)

    await sync_user_links_if_stale(db, user)
    await db.commit()

    tariffs = await _load_tariffs(db, user.tariff_category)
    row = build_account_row(user)
    ctx = {
        **_cabinet_nav(user),
        "row": row,
        "tariffs": tariffs,
        "message": request.query_params.get("msg"),
        "error": request.query_params.get("err"),
    }
    return templates.TemplateResponse(request, "cabinet.html", ctx)


@router.post("/cabinet/save")
async def cabinet_save(
    request: Request,
    email: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Обновляет только почту пользователя (пароль меняется через email-ссылку)."""
    user = await require_user_from_cookie(request, db)
    old_email = user.email
    email_clean = email.strip() or None
    try:
        await update_vpn_account(
            db,
            actor=user,
            user_id=user.id,
            email=email.strip(),
        )
        email_changed = email_clean != old_email
        if email_clean and (email_changed or not user.email_verified_at):
            raw = await create_email_verification_token(db, user)
            verify_url = f"{settings.site_base_url.rstrip('/')}/verify-email?token={raw}"
            email_service.send_email_verification(user.email, verify_url)
        await db.commit()
        return RedirectResponse(url="/cabinet?msg=Сохранено", status_code=303)
    except AccountError as exc:
        return RedirectResponse(url=f"/cabinet?err={exc}", status_code=303)


@router.post("/cabinet/request-password-reset")
async def cabinet_request_password_reset(request: Request, db: AsyncSession = Depends(get_db)):
    """Отправляет на почту пользователя ссылку для смены пароля."""
    user = await require_user_from_cookie(request, db)
    if not user.email:
        return RedirectResponse(
            url="/cabinet?err=Сначала+укажите+почту",
            status_code=303,
        )

    from app.api.auth import forgot_password as api_forgot_password
    from app.schemas.auth import ForgotPasswordRequest

    await api_forgot_password(ForgotPasswordRequest(email=user.email), db)
    return RedirectResponse(
        url="/cabinet?msg=Ссылка+для+смены+пароля+отправлена+на+почту",
        status_code=303,
    )


@router.post("/cabinet/resend-verify")
async def cabinet_resend_verify(request: Request, db: AsyncSession = Depends(get_db)):
    user = await require_user_from_cookie(request, db)
    if not user.email:
        return RedirectResponse(url="/cabinet?err=Сначала+укажите+почту", status_code=303)
    raw = await create_email_verification_token(db, user)
    verify_url = f"{settings.site_base_url.rstrip('/')}/verify-email?token={raw}"
    email_service.send_email_verification(user.email, verify_url)
    await db.commit()
    return RedirectResponse(url="/cabinet?msg=Письмо+отправлено", status_code=303)


@router.post("/pay/create")
async def pay_create(
    request: Request,
    tariff_id: int = Form(...),
    target_user_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await require_user_from_cookie(request, db)
    target_id = UUID(target_user_id) if target_user_id.strip() else None

    try:
        payment, confirmation_url = await create_payment_for_user(
            db, user, tariff_id, target_user_id=target_id
        )
        await db.commit()
    except BillingError as exc:
        panel = role_home_url(user.role)
        return RedirectResponse(url=f"{panel}?err={exc}", status_code=303)

    return RedirectResponse(url=confirmation_url, status_code=303)


@router.get("/pay/stub/{payment_id}", response_class=HTMLResponse)
async def pay_stub_page(payment_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    user = await require_user_from_cookie(request, db)
    result = await db.execute(
        select(Payment)
        .where(Payment.id == payment_id)
        .options(selectinload(Payment.tariff))
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=404, detail="Платёж не найден")

    from app.services.acl import can_manage_user

    beneficiary_result = await db.execute(select(User).where(User.id == payment.user_id))
    beneficiary = beneficiary_result.scalar_one_or_none()
    if beneficiary is None or not await can_manage_user(db, user, beneficiary):
        raise HTTPException(status_code=403, detail="Нет доступа к платежу")

    return templates.TemplateResponse(
        request,
        "pay_stub.html",
        {"payment": payment, "stub_mode": yukassa_service.is_stub, "user": user},
    )


@router.post("/pay/stub/{payment_id}/confirm")
async def pay_stub_confirm(payment_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    user = await require_user_from_cookie(request, db)
    result = await db.execute(select(Payment).where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=404, detail="Платёж не найден")

    from app.services.acl import can_manage_user

    beneficiary_result = await db.execute(select(User).where(User.id == payment.user_id))
    beneficiary = beneficiary_result.scalar_one_or_none()
    if beneficiary is None or not await can_manage_user(db, user, beneficiary):
        raise HTTPException(status_code=403, detail="Нет доступа к платежу")

    if payment.status != PaymentStatus.pending:
        return RedirectResponse(url="/pay/success", status_code=303)

    try:
        await process_payment_succeeded(
            db,
            yukassa_payment_id=payment.yukassa_payment_id,
            amount=payment.amount,
        )
        await db.commit()
    except BillingError as exc:
        return RedirectResponse(url=f"{role_home_url(user.role)}?err={exc}", status_code=303)

    return RedirectResponse(url="/pay/success", status_code=303)


@router.get("/pay/success", response_class=HTMLResponse)
async def pay_success(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "success.html", {"user": user})


@router.get("/docs/windows", response_class=HTMLResponse)
async def docs_windows(request: Request, db: AsyncSession = Depends(get_db)):
    user = await require_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "docs/windows.html", {"user": user})


@router.get("/docs/android", response_class=HTMLResponse)
async def docs_android(request: Request, db: AsyncSession = Depends(get_db)):
    user = await require_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "docs/android.html", {"user": user})


@router.get("/docs/ios", response_class=HTMLResponse)
async def docs_ios(request: Request, db: AsyncSession = Depends(get_db)):
    user = await require_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "docs/ios.html", {"user": user})

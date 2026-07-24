from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies import require_admin
from app.db.session import get_db
from app.models.marzban_job import MarzbanJob
from app.models.payment import Payment, PaymentStatus
from app.models.user import User, UserAccountLink
from app.services.billing import BillingError, process_payment_succeeded
from app.services.yukassa import yukassa_service
from app.schemas.admin import (
    BridgeStatusResponse,
    ConnectionDetailResponse,
    ExtendConnectionRequest,
    ExtendConnectionResponse,
    MarzbanJobResponse,
    SyncAllResponse,
)
from app.services.connections import build_connection_info, sync_account_link
from app.services.marzban import MarzbanError, marzban_client
from app.services.marzban_jobs import extend_with_fallback, sync_all_links

router = APIRouter(prefix="/admin", tags=["admin"])


async def _get_link_or_404(db: AsyncSession, connection_name: str) -> UserAccountLink:
    result = await db.execute(
        select(UserAccountLink).where(UserAccountLink.account_username == connection_name)
    )
    link = result.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Подключение не найдено")
    return link


@router.get("/bridge/status", response_model=BridgeStatusResponse)
async def bridge_status(_: User = Depends(require_admin)) -> BridgeStatusResponse:
    from app.config import get_settings

    settings = get_settings()
    try:
        result = await marzban_client.health_check()
        return BridgeStatusResponse(
            reachable=result["reachable"],
            token_obtained=result["token_obtained"],
            base_url=settings.marzban_base_url,
        )
    except MarzbanError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/connections/{connection_name}", response_model=ConnectionDetailResponse)
async def get_connection(
    connection_name: str,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ConnectionDetailResponse:
    link = await _get_link_or_404(db, connection_name)
    try:
        panel_user = await sync_account_link(db, link)
    except MarzbanError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return ConnectionDetailResponse(
        connection_name=connection_name,
        panel_data=panel_user,
        cached=build_connection_info(link),
        last_synced_at=link.last_synced_at,
    )


@router.post("/connections/{connection_name}/extend", response_model=ExtendConnectionResponse)
async def extend_connection(
    connection_name: str,
    body: ExtendConnectionRequest,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ExtendConnectionResponse:
    link = await _get_link_or_404(db, connection_name)
    result = await extend_with_fallback(db, link, body.period_days)

    return ExtendConnectionResponse(
        connection_name=connection_name,
        mode=result["mode"],
        new_expire=result.get("new_expire"),
        job_id=result.get("job_id"),
        queued=result["queued"],
        error=result.get("error"),
    )


@router.post("/connections/sync-all", response_model=SyncAllResponse)
async def sync_connections(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SyncAllResponse:
    result = await sync_all_links(db)
    return SyncAllResponse(**result)


@router.post("/import-marzban")
async def import_marzban_users(
    actor: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Импорт VPN-пользователей из Marzban в VXN_Pay."""
    from app.services.accounts import AccountError, import_users_from_marzban

    try:
        stats = await import_users_from_marzban(db, actor=actor)
        await db.commit()
    except AccountError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return stats


@router.get("/jobs", response_model=list[MarzbanJobResponse])
async def list_jobs(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[MarzbanJobResponse]:
    result = await db.execute(select(MarzbanJob).order_by(MarzbanJob.id.desc()).limit(50))
    return list(result.scalars().all())


@router.post("/payments/{payment_id}/simulate")
async def simulate_payment(
    payment_id: UUID,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not yukassa_service.is_stub:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Симуляция доступна только в режиме заглушки ЮKassa",
        )

    result = await db.execute(select(Payment).where(Payment.id == payment_id).options(selectinload(Payment.tariff)))
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Платёж не найден")
    if payment.status != PaymentStatus.pending:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Платёж уже обработан")

    try:
        outcome = await process_payment_succeeded(
            db,
            yukassa_payment_id=payment.yukassa_payment_id,
            amount=payment.amount,
        )
    except BillingError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return outcome

import logging
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies import get_current_user
from app.db.session import get_db
from app.models.payment import Payment
from app.models.tariff import Tariff
from app.models.user import User
from app.schemas.payment import PaymentCreateRequest, PaymentCreateResponse, PaymentResponse, TariffResponse
from app.services.billing import BillingError, create_payment_for_user, process_payment_succeeded
from app.services.yukassa import yukassa_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("/tariffs", response_model=list[TariffResponse])
async def list_tariffs(db: AsyncSession = Depends(get_db)) -> list[Tariff]:
    result = await db.execute(select(Tariff).where(Tariff.is_active.is_(True)).order_by(Tariff.period_days))
    return list(result.scalars().all())


@router.post("/create", response_model=PaymentCreateResponse)
async def create_payment(
    body: PaymentCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaymentCreateResponse:
    try:
        payment, confirmation_url = await create_payment_for_user(db, current_user, body.tariff_id)
        await db.refresh(payment, attribute_names=["tariff"])
    except BillingError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return PaymentCreateResponse(
        payment_id=payment.id,
        confirmation_url=confirmation_url,
        amount=payment.amount,
        tariff_name=payment.tariff.name,
        stub_mode=yukassa_service.is_stub,
    )


@router.get("/{payment_id}", response_model=PaymentResponse)
async def get_payment(
    payment_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaymentResponse:
    result = await db.execute(
        select(Payment)
        .where(Payment.id == payment_id, Payment.user_id == current_user.id)
        .options(selectinload(Payment.tariff))
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Платёж не найден")

    return PaymentResponse(
        id=payment.id,
        status=payment.status.value,
        amount=payment.amount,
        tariff_name=payment.tariff.name,
        paid_at=payment.paid_at,
        marzban_extended=payment.marzban_extended,
        created_at=payment.created_at,
    )


@router.post("/yukassa/webhook")
async def yukassa_webhook(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    body = await request.json()
    event = body.get("event")
    obj = body.get("object") or {}

    if event != "payment.succeeded":
        return {"status": "ignored"}

    yukassa_id = obj.get("id")
    if not yukassa_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing payment id")

    amount_value = obj.get("amount", {}).get("value")
    amount = Decimal(amount_value) if amount_value else None

    try:
        await process_payment_succeeded(db, yukassa_payment_id=yukassa_id, amount=amount)
    except BillingError as exc:
        logger.error("Webhook billing error: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return {"status": "ok"}

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.payment import Payment, PaymentStatus
from app.models.tariff import Tariff
from app.models.user import User
from app.services.marzban_jobs import extend_with_fallback
from app.services.yukassa import yukassa_service

logger = logging.getLogger(__name__)


class BillingError(Exception):
    pass


async def get_tariff(db: AsyncSession, tariff_id: int) -> Tariff:
    result = await db.execute(select(Tariff).where(Tariff.id == tariff_id, Tariff.is_active.is_(True)))
    tariff = result.scalar_one_or_none()
    if tariff is None:
        raise BillingError("Тариф не найден или неактивен")
    return tariff


async def create_payment_for_user(db: AsyncSession, user: User, tariff_id: int) -> tuple[Payment, str]:
    from app.config import get_settings

    settings = get_settings()
    tariff = await get_tariff(db, tariff_id)

    if not user.account_links:
        raise BillingError("Нет привязанных учётных записей для продления")

    payment = Payment(
        user_id=user.id,
        tariff_id=tariff.id,
        amount=tariff.price_rub,
        status=PaymentStatus.pending,
        yukassa_payment_id=f"pending-{uuid.uuid4()}",
    )
    db.add(payment)
    await db.flush()

    yk_response = await yukassa_service.create_payment(
        payment_id=payment.id,
        amount=tariff.price_rub,
        description=settings.payment_description,
        user_id=user.id,
        tariff_id=tariff.id,
    )

    payment.yukassa_payment_id = yk_response["id"]
    await db.flush()

    confirmation_url = yk_response["confirmation"]["confirmation_url"]
    return payment, confirmation_url


async def process_payment_succeeded(
    db: AsyncSession,
    *,
    yukassa_payment_id: str,
    amount: Decimal | None = None,
) -> dict[str, Any]:
    result = await db.execute(
        select(Payment)
        .where(Payment.yukassa_payment_id == yukassa_payment_id)
        .options(
            selectinload(Payment.tariff),
        )
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        raise BillingError(f"Платёж {yukassa_payment_id} не найден")

    if payment.status == PaymentStatus.succeeded:
        logger.info("Платёж %s уже обработан (идемпотентность)", yukassa_payment_id)
        return {"status": "already_processed", "payment_id": str(payment.id)}

    if amount is not None and amount != payment.amount:
        raise BillingError("Сумма платежа не совпадает с тарифом")

    user_result = await db.execute(
        select(User).where(User.id == payment.user_id).options(selectinload(User.account_links))
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        raise BillingError("Пользователь платежа не найден")

    extend_results = []
    all_extended = True
    for link in user.account_links:
        result_extend = await extend_with_fallback(
            db,
            link,
            payment.tariff.period_days,
            payment_id=payment.id,
        )
        extend_results.append({"connection": link.account_username, **result_extend})
        if result_extend.get("queued") or result_extend.get("mode") == "queued":
            all_extended = False

    payment.status = PaymentStatus.succeeded
    payment.paid_at = datetime.now(UTC)
    payment.marzban_extended = all_extended
    await db.flush()

    logger.info("Платёж %s успешно обработан", yukassa_payment_id)
    return {
        "status": "processed",
        "payment_id": str(payment.id),
        "marzban_extended": all_extended,
        "connections": extend_results,
    }

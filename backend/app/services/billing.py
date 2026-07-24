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
from app.models.user import TariffCategory, User
from app.services.acl import can_manage_user
from app.services.marzban_jobs import extend_with_fallback
from app.services.yukassa import yukassa_service

logger = logging.getLogger(__name__)


class BillingError(Exception):
    pass


async def get_tariff(db: AsyncSession, tariff_id: int, category: TariffCategory) -> Tariff:
    """Возвращает активный тариф, соответствующий категории пользователя."""
    result = await db.execute(
        select(Tariff).where(
            Tariff.id == tariff_id,
            Tariff.is_active.is_(True),
            Tariff.category == category,
        )
    )
    tariff = result.scalar_one_or_none()
    if tariff is None:
        raise BillingError("Тариф не найден, неактивен или не подходит категории")
    return tariff


async def create_payment_for_user(
    db: AsyncSession,
    payer: User,
    tariff_id: int,
    *,
    target_user_id: UUID | None = None,
) -> tuple[Payment, str]:
    """
    Создаёт платёж и redirect на ЮKassa.

    target_user_id — чей VPN продлевается (по умолчанию сам payer).
    """
    from app.config import get_settings

    settings = get_settings()

    beneficiary_id = target_user_id or payer.id
    if beneficiary_id != payer.id:
        target_result = await db.execute(select(User).where(User.id == beneficiary_id))
        target_user = target_result.scalar_one_or_none()
        if target_user is None:
            raise BillingError("Целевой пользователь не найден")
        if not await can_manage_user(db, payer, target_user):
            raise BillingError("Недостаточно прав для оплаты за этого пользователя")
        beneficiary = target_user
    else:
        beneficiary = payer

    tariff = await get_tariff(db, tariff_id, beneficiary.tariff_category)

    if not beneficiary.account_links:
        raise BillingError("Нет привязанных учётных записей для продления")

    payment = Payment(
        user_id=beneficiary.id,
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
        user_id=beneficiary.id,
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

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class PaymentCreateRequest(BaseModel):
    tariff_id: int


class PaymentCreateResponse(BaseModel):
    payment_id: UUID
    confirmation_url: str
    amount: Decimal
    tariff_name: str
    stub_mode: bool


class PaymentResponse(BaseModel):
    id: UUID
    status: str
    amount: Decimal
    tariff_name: str
    paid_at: datetime | None
    marzban_extended: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TariffResponse(BaseModel):
    id: int
    name: str
    period_days: int
    price_rub: Decimal

    model_config = {"from_attributes": True}

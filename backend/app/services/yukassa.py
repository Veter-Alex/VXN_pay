import uuid
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

from app.config import get_settings

settings = get_settings()


class YukassaError(Exception):
    pass


class YukassaService:
    @property
    def is_stub(self) -> bool:
        if settings.yukassa_stub_mode:
            return True
        return not (settings.yukassa_shop_id and settings.yukassa_secret_key)

    def stub_payment_id(self, payment_id: UUID) -> str:
        return f"stub-{payment_id}"

    def stub_confirmation_url(self, payment_id: UUID) -> str:
        return f"{settings.site_base_url.rstrip('/')}/pay/stub/{payment_id}"

    async def create_payment(
        self,
        *,
        payment_id: UUID,
        amount: Decimal,
        description: str,
        user_id: UUID,
        tariff_id: int,
    ) -> dict[str, Any]:
        if self.is_stub:
            yk_id = self.stub_payment_id(payment_id)
            return {
                "id": yk_id,
                "status": "pending",
                "confirmation": {
                    "type": "redirect",
                    "confirmation_url": self.stub_confirmation_url(payment_id),
                },
            }

        payload = {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "confirmation": {
                "type": "redirect",
                "return_url": f"{settings.site_base_url.rstrip('/')}/pay/success",
            },
            "capture": True,
            "description": description,
            "metadata": {
                "user_id": str(user_id),
                "tariff_id": str(tariff_id),
                "internal_payment_id": str(payment_id),
            },
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{settings.yukassa_api_url.rstrip('/')}/payments",
                json=payload,
                auth=(settings.yukassa_shop_id, settings.yukassa_secret_key),
                headers={"Idempotence-Key": str(uuid.uuid4())},
            )

        if response.status_code >= 400:
            raise YukassaError(f"ЮKassa API error {response.status_code}: {response.text}")

        return response.json()


yukassa_service = YukassaService()

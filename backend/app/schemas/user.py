from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.models.user import TariffCategory, UserRole


class ConnectionInfo(BaseModel):
    name: str
    status: str = "pending_sync"
    expires_at: datetime | None = None
    data_limit_bytes: int | None = None
    data_used_bytes: int | None = None
    subscription_url: str | None = None


class UserMeResponse(BaseModel):
    id: UUID
    login: str
    email: EmailStr | None = None
    phone: str | None
    role: UserRole
    tariff_category: TariffCategory
    email_verified_at: datetime | None = None
    password_set: bool = True
    connections: list[ConnectionInfo]
    created_at: datetime

    model_config = {"from_attributes": True}


class UserMeUpdateRequest(BaseModel):
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    old_password: str | None = Field(default=None, min_length=1)
    new_password: str | None = Field(default=None, min_length=8, max_length=128)


class UserCreateRequest(BaseModel):
    login: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    account_usernames: list[str] = Field(min_length=1)
    comments: str | None = None
    role: UserRole = UserRole.user
    tariff_category: TariffCategory = TariffCategory.commercial


class UserCreateResponse(BaseModel):
    id: UUID
    login: str
    email: EmailStr | None = None
    phone: str | None
    role: UserRole
    tariff_category: TariffCategory
    account_usernames: list[str]
    comments: str | None
    created_at: datetime

    model_config = {"from_attributes": True}

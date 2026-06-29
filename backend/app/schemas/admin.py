from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.user import ConnectionInfo


class BridgeStatusResponse(BaseModel):
    reachable: bool
    token_obtained: bool
    base_url: str


class ExtendConnectionRequest(BaseModel):
    period_days: int = Field(ge=1, le=3650)


class ExtendConnectionResponse(BaseModel):
    connection_name: str
    mode: str
    new_expire: int | None = None
    job_id: int | None = None
    queued: bool
    error: str | None = None


class ConnectionDetailResponse(BaseModel):
    connection_name: str
    panel_data: dict
    cached: ConnectionInfo
    last_synced_at: datetime | None


class SyncAllResponse(BaseModel):
    synced: int
    total: int
    errors: list[dict[str, str]]


class MarzbanJobResponse(BaseModel):
    id: int
    account_username: str
    payment_id: UUID | None
    payload: dict
    attempts: int
    max_attempts: int
    next_retry_at: datetime
    status: str
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}

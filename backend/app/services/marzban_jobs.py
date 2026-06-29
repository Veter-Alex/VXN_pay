import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import async_session_factory
from app.models.marzban_job import MarzbanJob, MarzbanJobStatus
from app.models.user import UserAccountLink
from app.services.connections import extend_account_link, sync_account_link
from app.services.marzban import MarzbanError, marzban_client

logger = logging.getLogger(__name__)
settings = get_settings()


async def enqueue_extend_job(
    db: AsyncSession,
    *,
    account_username: str,
    period_days: int,
    payment_id: UUID | None = None,
    error_message: str | None = None,
) -> MarzbanJob:
    now = datetime.now(UTC)
    job = MarzbanJob(
        payment_id=payment_id,
        account_username=account_username,
        payload={"action": "extend", "period_days": period_days},
        attempts=0,
        max_attempts=settings.marzban_job_max_attempts,
        next_retry_at=now,
        status=MarzbanJobStatus.pending,
        error_message=error_message,
    )
    db.add(job)
    await db.flush()
    return job


async def _get_link(db: AsyncSession, account_username: str) -> UserAccountLink | None:
    result = await db.execute(
        select(UserAccountLink).where(UserAccountLink.account_username == account_username)
    )
    return result.scalar_one_or_none()


async def process_job(db: AsyncSession, job: MarzbanJob) -> None:
    action = job.payload.get("action")
    if action != "extend":
        job.status = MarzbanJobStatus.failed
        job.error_message = f"Неизвестное действие: {action}"
        return

    period_days = int(job.payload["period_days"])
    link = await _get_link(db, job.account_username)
    if link is None:
        job.status = MarzbanJobStatus.failed
        job.error_message = "Привязка учётной записи не найдена"
        return

    await extend_account_link(db, link, period_days)
    job.status = MarzbanJobStatus.done
    job.error_message = None


async def process_pending_jobs() -> int:
    now = datetime.now(UTC)
    processed = 0

    async with async_session_factory() as db:
        result = await db.execute(
            select(MarzbanJob)
            .where(
                MarzbanJob.status == MarzbanJobStatus.pending,
                MarzbanJob.next_retry_at <= now,
            )
            .order_by(MarzbanJob.id)
            .limit(20)
        )
        jobs = result.scalars().all()

        for job in jobs:
            job.attempts += 1
            try:
                await process_job(db, job)
                processed += 1
            except MarzbanError as exc:
                job.error_message = str(exc)
                if job.attempts >= job.max_attempts:
                    job.status = MarzbanJobStatus.failed
                    logger.error("Job %s failed permanently: %s", job.id, exc)
                else:
                    job.next_retry_at = now + timedelta(seconds=settings.marzban_job_retry_delay_seconds)
                    logger.warning("Job %s retry %s/%s: %s", job.id, job.attempts, job.max_attempts, exc)
            except Exception as exc:
                job.error_message = str(exc)
                if job.attempts >= job.max_attempts:
                    job.status = MarzbanJobStatus.failed
                else:
                    job.next_retry_at = now + timedelta(seconds=settings.marzban_job_retry_delay_seconds)
                logger.exception("Job %s unexpected error", job.id)

        await db.commit()

    return processed


async def extend_with_fallback(
    db: AsyncSession,
    link: UserAccountLink,
    period_days: int,
    *,
    payment_id: UUID | None = None,
) -> dict[str, Any]:
    try:
        new_expire = await extend_account_link(db, link, period_days)
        return {"mode": "direct", "new_expire": new_expire, "queued": False}
    except MarzbanError as exc:
        job = await enqueue_extend_job(
            db,
            account_username=link.account_username,
            period_days=period_days,
            payment_id=payment_id,
            error_message=str(exc),
        )
        return {"mode": "queued", "job_id": job.id, "queued": True, "error": str(exc)}


async def sync_all_links(db: AsyncSession) -> dict[str, Any]:
    result = await db.execute(select(UserAccountLink))
    links = result.scalars().all()

    synced = 0
    errors: list[dict[str, str]] = []

    for link in links:
        try:
            await sync_account_link(db, link)
            synced += 1
        except MarzbanError as exc:
            errors.append({"name": link.account_username, "error": str(exc)})

    return {"synced": synced, "total": len(links), "errors": errors}

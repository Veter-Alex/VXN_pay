import asyncio
import logging

from app.config import get_settings
from app.services.marzban_jobs import process_pending_jobs

logger = logging.getLogger(__name__)
settings = get_settings()


async def marzban_job_worker(stop_event: asyncio.Event) -> None:
    logger.info("Marzban job worker started (interval=%ss)", settings.marzban_job_poll_interval_seconds)
    while not stop_event.is_set():
        try:
            processed = await process_pending_jobs()
            if processed:
                logger.info("Processed %s marzban jobs", processed)
        except Exception:
            logger.exception("Marzban job worker iteration failed")

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.marzban_job_poll_interval_seconds,
            )
        except TimeoutError:
            continue

    logger.info("Marzban job worker stopped")

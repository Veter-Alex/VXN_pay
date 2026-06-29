import asyncio
import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import select

from app.api.admin import router as admin_router
from app.api.auth import limiter, router as auth_router
from app.api.payments import router as payments_router
from app.api.users import router as users_router
from app.web.routes import router as web_router
from app.workers.marzban_worker import marzban_job_worker
from app.config import get_settings
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)
settings = get_settings()


async def bootstrap_admin() -> None:
    if not settings.bootstrap_admin_login or not settings.bootstrap_admin_password:
        logger.warning("BOOTSTRAP_ADMIN_* не заданы — первый admin не будет создан автоматически")
        return

    async with async_session_factory() as session:
        result = await session.execute(select(User).where(User.role == UserRole.admin))
        if result.scalar_one_or_none() is not None:
            return

        admin = User(
            login=settings.bootstrap_admin_login,
            password_hash=hash_password(settings.bootstrap_admin_password),
            email=settings.bootstrap_admin_email or f"{settings.bootstrap_admin_login}@localhost",
            role=UserRole.admin,
        )
        session.add(admin)
        await session.commit()
        logger.info("Bootstrap admin '%s' создан", settings.bootstrap_admin_login)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await bootstrap_admin()
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(marzban_job_worker(stop_event))
    yield
    stop_event.set()
    await worker_task
    await engine.dispose()


app = FastAPI(title="VXN Pay API", version="0.1.0", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.include_router(web_router)
app.include_router(auth_router, prefix="/api/v1")
app.include_router(users_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(payments_router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

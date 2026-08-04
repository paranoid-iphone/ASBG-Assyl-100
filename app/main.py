import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from .admin import router as admin_router
from .bot import BOT_RUNTIME, run_bot
from .config import get_settings
from .database import Base, engine, ensure_schema_compatibility
from .models import Event
from .display import router as display_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    ensure_schema_compatibility()
    settings = get_settings()
    task = asyncio.create_task(run_bot(settings.telegram_bot_token)) if settings.telegram_bot_token else None
    yield
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(admin_router)
app.include_router(display_router)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "bot_enabled": bool(settings.telegram_bot_token),
        "bot_connected": BOT_RUNTIME["connected"],
        "bot_username": BOT_RUNTIME["username"],
        "bot_error": BOT_RUNTIME["error"],
    }

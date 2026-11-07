"""FastAPI entry point. Run: uvicorn app.main:app --reload"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse

load_dotenv()

from app import autopilot, store  # noqa: E402
from app.routers import api  # noqa: E402

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Create and seed the database on startup."""
    store.init_db()
    task = asyncio.create_task(autopilot.loop())
    yield
    task.cancel()


app = FastAPI(title="Agent Allowance", lifespan=lifespan)
app.include_router(api.router)


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    """Serve the web UI."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    """Health check."""
    return {"app": "Agent Allowance", "docs": "/docs"}

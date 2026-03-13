"""
Agent Transcript Editor - Backend API

FastAPI server providing:
- Transcript file management and AI-assisted editing
- Trusted monitor evaluation
- SSE-based agent loop with HTTP commands
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Configure logging so our modules' INFO messages show up alongside uvicorn output
logging.basicConfig(
    level=logging.INFO,
    format="\033[90m%(asctime)s\033[0m %(levelname)s \033[36m%(name)s\033[0m %(message)s",
    datefmt="%H:%M:%S",
)
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from rate_limit import get_all_status, set_limit, set_on_rate_limit_wait  # noqa: E402
from routers import files, llm, monitor, sse, tools  # noqa: E402
from sessions import session_manager  # noqa: E402

# Load environment variables
load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure transcripts directory exists
    transcripts_dir = Path(os.getenv("TRANSCRIPTS_DIR", "./transcripts"))
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    # Ensure default/ project subdirectory exists
    default_dir = transcripts_dir / "default"
    default_dir.mkdir(exist_ok=True)

    # Migrate loose .jsonl files from root into default/
    for f in transcripts_dir.glob("*.jsonl"):
        dest = default_dir / f.name
        if not dest.exists():
            f.rename(dest)

    # Wire up rate limit wait broadcast — notifies all SSE subscribers when rate limited
    async def _broadcast_rate_limit(key_id: str, wait_seconds: float, used: int, limit: int):
        for s in session_manager._sessions.values():
            if s.is_streaming and s.api_key_id == key_id:
                await session_manager.broadcast(
                    s,
                    {
                        "type": "rate_limit_wait",
                        "key_id": key_id,
                        "wait_seconds": round(wait_seconds, 1),
                        "used_tpm": used,
                        "limit_tpm": limit,
                    },
                )

    set_on_rate_limit_wait(_broadcast_rate_limit)

    yield
    # Shutdown: flush all sessions to disk
    await session_manager.flush_all()


app = FastAPI(
    title="Transcript Editor API",
    description="Backend for Agent Transcript Editor - AI transcript editing tool",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS configuration — override with CORS_ORIGINS env var (comma-separated)
_default_origins = [
    "http://localhost:5173",  # Vite dev server
    "http://localhost:4173",  # Vite preview
    "http://127.0.0.1:5173",
    "http://127.0.0.1:4173",
]
_cors_origins = os.getenv("CORS_ORIGINS")
_origins = [o.strip() for o in _cors_origins.split(",")] if _cors_origins else _default_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(llm.router, prefix="/api/llm", tags=["LLM"])
app.include_router(files.router, prefix="/api/files", tags=["Files"])
app.include_router(monitor.router, prefix="/api/monitor", tags=["Monitor"])
app.include_router(tools.router, prefix="/api/tools", tags=["Tools"])
app.include_router(sse.router, prefix="/api/session", tags=["Session"])


@app.get("/api/health")
async def health_check():
    """Health check endpoint with diagnostics for debugging setup issues."""
    has_api_key = bool(os.getenv("ANTHROPIC_API_KEY"))
    has_alt_key = bool(os.getenv("ANTHROPIC_API_KEY_ALT"))

    # Check trusted-monitor availability
    monitor_available = False
    try:
        from trusted_monitor.monitor import create_monitor  # noqa: F401

        monitor_available = True
    except ImportError:
        pass

    # Active sessions and rate limit status
    active_sessions = len(session_manager._sessions)
    streaming_sessions = sum(1 for s in session_manager._sessions.values() if s.is_streaming)
    rate_limits = get_all_status()

    return {
        "status": "ok" if has_api_key else "missing_api_key",
        "has_api_key": has_api_key,
        "has_alt_key": has_alt_key,
        "monitor_available": monitor_available,
        "active_sessions": active_sessions,
        "streaming_sessions": streaming_sessions,
        "rate_limits": rate_limits,
    }


class RateLimitConfig(BaseModel):
    key_id: str
    tpm: int


@app.get("/api/config/rate-limit")
async def get_rate_limit():
    """Return per-key TPM rate limit status."""
    return get_all_status()


@app.put("/api/config/rate-limit")
async def put_rate_limit(config: RateLimitConfig):
    """Update TPM limit for a specific key."""
    if config.tpm < 10_000:
        raise HTTPException(status_code=422, detail="tpm must be at least 10,000")
    set_limit(config.key_id, config.tpm)
    return get_all_status()


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=True,
        reload_dirs=[str(Path(__file__).resolve().parent)],
    )

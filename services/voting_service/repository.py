"""Repository factory for Voting Service."""

import os
from typing import Optional

from app.ports.session_repository import SessionRepository

# Try Redis first, fallback to Postgres, then File
REDIS_URL = os.getenv("REDIS_URL")
POSTGRES_DSN = os.getenv("POSTGRES_DSN")
STATE_FILE = os.getenv("STATE_FILE", "data/state.json")


async def get_repository() -> SessionRepository:
    """Get session repository based on configuration."""
    if REDIS_URL:
        try:
            from services.voting_service.redis_repository import RedisSessionRepository
            return RedisSessionRepository(REDIS_URL)
        except ImportError:
            print("[Voting] Redis not available, falling back to Postgres")
    
    if POSTGRES_DSN:
        try:
            from services.voting_service.postgres_repository import PostgresSessionRepository
            return await PostgresSessionRepository.create(POSTGRES_DSN)
        except ImportError:
            print("[Voting] Postgres not available, falling back to File")
    
    # Fallback to file-based
    from pathlib import Path
    from app.adapters.session_file import FileSessionRepository
    return FileSessionRepository(Path(STATE_FILE))

"""Metrics endpoints."""

from fastapi import APIRouter, Request

router = APIRouter()
metrics_router = router  # alias for main.py


@router.get("/", response_model=dict)
async def get_metrics(request: Request) -> dict:
    """Aggregate CMS read-model counters when Postgres is available."""
    cms_store = getattr(request.app.state, "cms_store", None)
    if cms_store is None:
        return {
            "sessions_count": 0,
            "active_sessions": 0,
            "total_votes": 0,
            "postgres_ready": False,
        }
    overview = await cms_store.overview(is_superuser=True)
    return {
        "sessions_count": int(overview.get("total_sessions") or 0),
        "active_sessions": int(overview.get("active_sessions") or 0),
        "total_votes": int(overview.get("total_votes") or 0),
        "total_users": int(overview.get("total_users") or 0),
        "live_retros": int(overview.get("live_retros") or 0),
        "postgres_ready": True,
    }

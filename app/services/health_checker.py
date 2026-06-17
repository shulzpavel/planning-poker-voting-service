"""Периодическая проверка доступности сервисов. Записывает в метрики без спама."""

import asyncio
import logging
import time
from typing import Any, Dict

import aiohttp

from app.ports.metrics_repository import MetricsRepository
from config import JIRA_SERVICE_URL, VOTING_SERVICE_URL

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 300  # 5 минут
HEALTH_TIMEOUT = 10


async def _check_service(
    session: aiohttp.ClientSession,
    name: str,
    url: str,
) -> Dict[str, Any]:
    """Проверить один сервис. Возвращает {ok: bool, error?: str, latency_ms?: int}."""
    start = time.monotonic()
    try:
        async with session.get(
            f"{url.rstrip('/')}/health/",
            timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT),
        ) as resp:
            latency_ms = int((time.monotonic() - start) * 1000)
            if resp.status == 200:
                return {"ok": True, "latency_ms": latency_ms}
            return {"ok": False, "error": f"status={resp.status}", "latency_ms": latency_ms}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout", "latency_ms": HEALTH_TIMEOUT * 1000}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "latency_ms": None}


async def run_health_checks(metrics: MetricsRepository) -> None:
    """Запустить одну итерацию проверки всех сервисов."""
    services = [
        ("voting-service", VOTING_SERVICE_URL or "http://voting-service:8002"),
        ("jira-service", JIRA_SERVICE_URL or "http://jira-service:8001"),
    ]
    async with aiohttp.ClientSession() as session:
        for name, url in services:
            try:
                result = await _check_service(session, name, url)
                await metrics.record_event(
                    event="service_health",
                    status="ok" if result["ok"] else "error",
                    payload={
                        "service": name,
                        "error": result.get("error"),
                        "latency_ms": result.get("latency_ms"),
                    },
                )
                if not result["ok"]:
                    logger.warning("Service %s unhealthy: %s", name, result.get("error"))
            except Exception as exc:
                logger.debug("Health check failed for %s: %s", name, exc)
                try:
                    await metrics.record_event(
                        event="service_health",
                        status="error",
                        payload={"service": name, "error": str(exc)[:200]},
                    )
                except Exception as metrics_exc:
                    logger.debug("Failed to record service health metric for %s: %s", name, metrics_exc)


async def health_check_loop(metrics: MetricsRepository) -> None:
    """Фоновая задача: проверка сервисов каждые HEALTH_CHECK_INTERVAL секунд."""
    await asyncio.sleep(60)  # Подождать 1 мин после старта
    while True:
        try:
            await run_health_checks(metrics)
        except Exception as exc:
            logger.debug("Health check loop error: %s", exc)
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)

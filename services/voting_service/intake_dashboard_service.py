"""Shared Redis snapshots and bounded background refresh/AI jobs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import aiohttp

from app.domain.intake_dashboard import build_intake_snapshot
from services.voting_service.intake_llm import PROMPT_VERSION, generate_intake_analysis

logger = logging.getLogger(__name__)
_RELEASE_LOCK = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"

class IntakeDashboardService:
    def __init__(self, redis, session, settings):
        self.redis, self.session, self.settings = redis, session, settings
        self.prefix = f"product-intake:v1:{settings.cache_id}"
        self.tasks: set[asyncio.Task] = set()

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _read(self, key):
        value = await self.redis.get(key)
        try:
            return json.loads(value) if value else None
        except (TypeError, ValueError):
            return None

    async def _launch(self, key, work):
        token = uuid.uuid4().hex
        if not await self.redis.set(f"{key}:lock", token, nx=True, ex=660):
            return False
        task = asyncio.create_task(self._run(key, token, work))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    async def _run(self, key, token, work):
        try:
            result = await asyncio.wait_for(work(), timeout=600)
            if await self.redis.get(f"{key}:lock") == token:
                await self.redis.setex(key, 604800, json.dumps(result, ensure_ascii=False))
                await self.redis.delete(f"{key}:error")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = str(exc) if re.fullmatch(r"intake_[a-z0-9_]+", str(exc)) else type(exc).__name__
            logger.warning("Intake background %s failed (%s)", "analysis" if ":ai:" in key else "source", code)
            try:
                if await self.redis.get(f"{key}:lock") == token:
                    await self.redis.setex(f"{key}:error", 60, "unavailable")
            except Exception:
                logger.warning("Intake error state could not be saved")
        finally:
            try:
                await self.redis.eval(_RELEASE_LOCK, 1, f"{key}:lock", token)
            except Exception:
                logger.warning("Intake lock will expire")

    async def _load_source(self):
        url = os.getenv("JIRA_SERVICE_URL", "http://localhost:8001").rstrip("/")
        async with self.session.post(f"{url}/api/v1/intake/source", json={"jql": self.settings.jql, "source_field": self.settings.source_field}, timeout=aiohttp.ClientTimeout(total=540)) as response:
            if response.status != 200:
                raise RuntimeError(f"intake_source_http_{response.status}")
            value = await response.json()
        if not isinstance(value, dict) or not isinstance(value.get("issues"), list) or not value.get("fetched_at"):
            raise RuntimeError("intake_source_invalid")
        if any(not isinstance(x, dict) or x.get("history_complete") is not True or not isinstance(x.get("segments"), list) for x in value["issues"]):
            raise RuntimeError("intake_source_incomplete")
        # Validate every report before replacing the last successful source.
        for days in (7, 30, 90):
            self._snapshot(value, days)
        return value

    def _snapshot(self, source, days):
        cfg = self.settings
        at = datetime.fromisoformat(source["fetched_at"].replace("Z", "+00:00"))
        snapshot = build_intake_snapshot(source["issues"], now=at, days=days, statuses=cfg.statuses, timezone=cfg.timezone,
                                         information_statuses=cfg.information_statuses, deferred_statuses=cfg.deferred_statuses, rejected_statuses=cfg.rejected_statuses)
        snapshot["source"] = {"label": cfg.title, "stage_name": cfg.stage_name, "timezone": cfg.timezone, "jira_url": source.get("jira_url"), "empty": not source["issues"]}
        return snapshot

    async def snapshot(self, days):
        key = f"{self.prefix}:source"
        source, error = await self._read(key), await self.redis.get(f"{key}:error")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(source["fetched_at"].replace("Z", "+00:00"))).total_seconds() if source else float("inf")
        stale = age > self.settings.refresh_seconds
        if stale and not error:
            await self._launch(key, self._load_source)
        if not source:
            if error:
                raise RuntimeError("intake_refresh_failed")
            return None
        value = self._snapshot(source, days)
        value["freshness"] = {"stale": stale, "refreshing": bool(await self.redis.get(f"{key}:lock")), "refresh_error": bool(error), "refresh_seconds": self.settings.refresh_seconds}
        return value

    async def analysis(self, days, *, start=False):
        snapshot = await self.snapshot(days)
        if snapshot is None or snapshot["freshness"]["stale"]:
            return None
        context = {k: v for k, v in snapshot.items() if k != "freshness"}
        digest = hashlib.sha256((PROMPT_VERSION + self.settings.model + json.dumps(context, sort_keys=True)).encode()).hexdigest()
        key = f"{self.prefix}:ai:{days}:{digest}"
        cached = await self._read(key)
        if cached:
            return cached
        if await self.redis.get(f"{key}:error"):
            raise RuntimeError("intake_analysis_failed")
        if start:
            await self._launch(key, lambda: generate_intake_analysis(self.session, snapshot, self.settings.model))
        return None

"""Integration tests for one-time CMS bootstrap admin seeding."""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from services.voting_service.cms_rbac import verify_password
from services.voting_service.cms_store import PostgresCmsStore

POSTGRES_DSN = os.getenv("TEST_POSTGRES_DSN") or os.getenv("POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="POSTGRES_DSN or TEST_POSTGRES_DSN required for bootstrap admin integration tests",
)


@pytest_asyncio.fixture
async def cms_store():
    store = await PostgresCmsStore.create(POSTGRES_DSN)
    yield store
    await store.pool.close()


async def _fetch_admin(store: PostgresCmsStore, username: str) -> dict | None:
    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, username, password_hash, is_active, is_superuser
            FROM cms_admin_accounts
            WHERE username = $1
            """,
            username,
        )
    return dict(row) if row else None


@pytest.mark.asyncio
async def test_bootstrap_admin_created_on_first_run(cms_store: PostgresCmsStore):
    username = f"bootstrap-{uuid.uuid4().hex}"
    password = "initial-bootstrap-password"

    await cms_store.ensure_access_defaults(username, password)

    admin = await _fetch_admin(cms_store, username)
    assert admin is not None
    assert admin["is_active"] is True
    assert admin["is_superuser"] is True
    assert verify_password(password, admin["password_hash"])


@pytest.mark.asyncio
async def test_bootstrap_admin_password_not_reset_on_restart(cms_store: PostgresCmsStore):
    username = f"bootstrap-{uuid.uuid4().hex}"
    initial_password = "rotated-password-1"
    leaked_password = "leaked-env-password"

    await cms_store.ensure_access_defaults(username, initial_password)
    await cms_store.ensure_access_defaults(username, leaked_password)

    admin = await _fetch_admin(cms_store, username)
    assert admin is not None
    assert verify_password(initial_password, admin["password_hash"])
    assert not verify_password(leaked_password, admin["password_hash"])


@pytest.mark.asyncio
async def test_bootstrap_admin_deactivation_persists(cms_store: PostgresCmsStore):
    username = f"bootstrap-{uuid.uuid4().hex}"
    password = "bootstrap-password"

    await cms_store.ensure_access_defaults(username, password)

    async with cms_store.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE cms_admin_accounts
            SET is_active = FALSE, updated_at = NOW()
            WHERE username = $1
            """,
            username,
        )

    await cms_store.ensure_access_defaults(username, password)

    admin = await _fetch_admin(cms_store, username)
    assert admin is not None
    assert admin["is_active"] is False


@pytest.mark.asyncio
async def test_bootstrap_admin_superuser_revoke_persists(cms_store: PostgresCmsStore):
    username = f"bootstrap-{uuid.uuid4().hex}"
    password = "bootstrap-password"

    await cms_store.ensure_access_defaults(username, password)

    async with cms_store.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE cms_admin_accounts
            SET is_superuser = FALSE, updated_at = NOW()
            WHERE username = $1
            """,
            username,
        )

    await cms_store.ensure_access_defaults(username, password)

    admin = await _fetch_admin(cms_store, username)
    assert admin is not None
    assert admin["is_superuser"] is False

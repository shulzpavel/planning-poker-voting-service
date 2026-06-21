"""CMS store mixin: schema bootstrap and migrations."""

from __future__ import annotations

import json
import os

import asyncpg

from services.voting_service.cms_rbac import (
    ALL_PERMISSION_KEYS,
    CMS_PAGE_DEFINITIONS,
    CMS_PERMISSION_DEFINITIONS,
    DEPRECATED_CMS_PAGE_KEYS,
    OPERATIONAL_VIEW_PERMISSIONS,
    PERM_ACCESS_MANAGE,
    PERM_ACCESS_VIEW,
    PERM_APP_SESSIONS_MANAGE,
    PERM_PLANNER_VIEW,
    PERM_SESSIONS_VIEW,
    PERM_TASKS_MANAGE,
    hash_password,
)


class SchemaMixin:
    """Pool lifecycle, DDL, and access-control seed data."""

def __init__(self, pool: asyncpg.Pool):
    self.pool = pool

@classmethod
async def create(cls, dsn: str) -> "PostgresCmsStore":
    pool_min = max(1, int(os.getenv("CMS_DB_POOL_MIN", "2")))
    pool_max = max(pool_min, int(os.getenv("CMS_DB_POOL_MAX", "20")))
    pool = await asyncpg.create_pool(dsn, min_size=pool_min, max_size=pool_max)
    store = cls(pool)
    await store.ensure_schema()
    return store

async def ensure_schema(self) -> None:
    async with self.pool.acquire() as conn:
        await conn.execute(
            """
            CREATE EXTENSION IF NOT EXISTS pg_trgm;

            CREATE TABLE IF NOT EXISTS cms_sessions (
                id BIGSERIAL PRIMARY KEY,
                session_key TEXT NOT NULL UNIQUE,
                chat_id BIGINT NOT NULL,
                topic_id BIGINT,
                current_task_index INTEGER NOT NULL DEFAULT 0,
                participants_count INTEGER NOT NULL DEFAULT 0,
                tasks_queue_count INTEGER NOT NULL DEFAULT 0,
                history_count INTEGER NOT NULL DEFAULT 0,
                last_batch_count INTEGER NOT NULL DEFAULT 0,
                total_tasks INTEGER NOT NULL DEFAULT 0,
                total_votes INTEGER NOT NULL DEFAULT 0,
                batch_completed BOOLEAN NOT NULL DEFAULT FALSE,
                is_active BOOLEAN NOT NULL DEFAULT FALSE,
                current_batch_id TEXT,
                current_batch_started_at TEXT,
                current_task_id TEXT,
                tasks_version INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                raw JSONB NOT NULL
            );
            ALTER TABLE cms_sessions
                ADD COLUMN IF NOT EXISTS current_task_id TEXT;
            ALTER TABLE cms_sessions
                ADD COLUMN IF NOT EXISTS tasks_version INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE cms_sessions
                ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
            ALTER TABLE cms_sessions
                ADD COLUMN IF NOT EXISTS title TEXT;
            CREATE INDEX IF NOT EXISTS idx_cms_sessions_title_trgm
                ON cms_sessions USING GIN ((lower(title)) gin_trgm_ops)
                WHERE title IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_cms_sessions_updated
                ON cms_sessions(updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_cms_sessions_active_updated
                ON cms_sessions(is_active, updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_cms_sessions_chat
                ON cms_sessions(chat_id, topic_id);
            CREATE INDEX IF NOT EXISTS idx_cms_sessions_session_key_trgm
                ON cms_sessions USING GIN (session_key gin_trgm_ops);
            CREATE INDEX IF NOT EXISTS idx_cms_sessions_batch_id_trgm
                ON cms_sessions USING GIN (current_batch_id gin_trgm_ops)
                WHERE current_batch_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_cms_sessions_alive_updated
                ON cms_sessions(updated_at DESC, id DESC)
                WHERE deleted_at IS NULL;

            CREATE TABLE IF NOT EXISTS cms_users (
                user_id BIGINT PRIMARY KEY,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                is_web BOOLEAN NOT NULL DEFAULT FALSE,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_cms_users_last_seen
                ON cms_users(last_seen_at DESC, user_id DESC);
            CREATE INDEX IF NOT EXISTS idx_cms_users_role
                ON cms_users(role);
            CREATE INDEX IF NOT EXISTS idx_cms_users_name_trgm
                ON cms_users USING GIN (name gin_trgm_ops);
            CREATE INDEX IF NOT EXISTS idx_cms_users_id_text_trgm
                ON cms_users USING GIN ((user_id::text) gin_trgm_ops);

            CREATE TABLE IF NOT EXISTS cms_session_participants (
                session_id BIGINT NOT NULL REFERENCES cms_sessions(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'session',
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (session_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cms_session_participants_user
                ON cms_session_participants(user_id);

            CREATE TABLE IF NOT EXISTS cms_tasks (
                id BIGSERIAL PRIMARY KEY,
                session_id BIGINT NOT NULL REFERENCES cms_sessions(id) ON DELETE CASCADE,
                task_uid TEXT NOT NULL DEFAULT '',
                bucket TEXT NOT NULL,
                bucket_index INTEGER NOT NULL,
                jira_key TEXT,
                summary TEXT NOT NULL DEFAULT '',
                url TEXT,
                story_points INTEGER,
                source TEXT NOT NULL DEFAULT 'jira',
                votes_count INTEGER NOT NULL DEFAULT 0,
                numeric_avg NUMERIC,
                numeric_max INTEGER,
                completed_at TEXT,
                jql TEXT,
                created_at_text TEXT,
                domain_updated_at TEXT,
                raw JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(session_id, bucket, bucket_index)
            );
            ALTER TABLE cms_tasks
                ADD COLUMN IF NOT EXISTS task_uid TEXT NOT NULL DEFAULT '';
            ALTER TABLE cms_tasks
                ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'jira';
            ALTER TABLE cms_tasks
                ADD COLUMN IF NOT EXISTS created_at_text TEXT;
            ALTER TABLE cms_tasks
                ADD COLUMN IF NOT EXISTS domain_updated_at TEXT;
            CREATE INDEX IF NOT EXISTS idx_cms_tasks_session_bucket
                ON cms_tasks(session_id, bucket, bucket_index, id);
            CREATE INDEX IF NOT EXISTS idx_cms_tasks_session_uid
                ON cms_tasks(session_id, task_uid);
            CREATE INDEX IF NOT EXISTS idx_cms_tasks_jira_key
                ON cms_tasks(jira_key);
            CREATE INDEX IF NOT EXISTS idx_cms_tasks_updated
                ON cms_tasks(updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_cms_tasks_summary_trgm
                ON cms_tasks USING GIN (summary gin_trgm_ops);
            CREATE INDEX IF NOT EXISTS idx_cms_tasks_jira_key_trgm
                ON cms_tasks USING GIN (jira_key gin_trgm_ops)
                WHERE jira_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_cms_tasks_uid_trgm
                ON cms_tasks USING GIN (task_uid gin_trgm_ops)
                WHERE task_uid <> '';

            CREATE TABLE IF NOT EXISTS cms_votes (
                id BIGSERIAL PRIMARY KEY,
                task_id BIGINT NOT NULL REFERENCES cms_tasks(id) ON DELETE CASCADE,
                session_id BIGINT NOT NULL REFERENCES cms_sessions(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                value TEXT NOT NULL,
                is_numeric BOOLEAN NOT NULL DEFAULT FALSE,
                numeric_value INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_cms_votes_task_id
                ON cms_votes(task_id, id);
            CREATE INDEX IF NOT EXISTS idx_cms_votes_session_id
                ON cms_votes(session_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_cms_votes_user_id
                ON cms_votes(user_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_cms_votes_id_desc
                ON cms_votes(id DESC);

            CREATE TABLE IF NOT EXISTS cms_web_tokens (
                id BIGSERIAL PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                token_prefix TEXT NOT NULL,
                chat_id BIGINT NOT NULL,
                topic_id BIGINT,
                session_key TEXT NOT NULL,
                participants_joined INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_cms_web_tokens_expires
                ON cms_web_tokens(expires_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_cms_web_tokens_session
                ON cms_web_tokens(session_key, id DESC);

            CREATE TABLE IF NOT EXISTS cms_web_participants (
                id BIGSERIAL PRIMARY KEY,
                token_hash TEXT NOT NULL,
                participant_id TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                chat_id BIGINT NOT NULL,
                topic_id BIGINT,
                joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                UNIQUE(token_hash, participant_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cms_web_participants_user
                ON cms_web_participants(user_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_cms_web_participants_token
                ON cms_web_participants(token_hash, id DESC);

            CREATE TABLE IF NOT EXISTS cms_audit_events (
                id BIGSERIAL PRIMARY KEY,
                ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                action TEXT NOT NULL,
                actor TEXT,
                status TEXT NOT NULL DEFAULT 'ok',
                ip TEXT,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb
            );
            CREATE INDEX IF NOT EXISTS idx_cms_audit_events_ts
                ON cms_audit_events(ts DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_cms_audit_events_action
                ON cms_audit_events(action, ts DESC);

            CREATE TABLE IF NOT EXISTS cms_permissions (
                key TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS cms_pages (
                key TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                permission_key TEXT NOT NULL REFERENCES cms_permissions(key),
                sort_order INTEGER NOT NULL DEFAULT 100,
                is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_cms_pages_sort
                ON cms_pages(sort_order, key);

            CREATE TABLE IF NOT EXISTS cms_roles (
                id BIGSERIAL PRIMARY KEY,
                key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                is_system BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS cms_role_permissions (
                role_id BIGINT NOT NULL REFERENCES cms_roles(id) ON DELETE CASCADE,
                permission_key TEXT NOT NULL REFERENCES cms_permissions(key) ON DELETE CASCADE,
                PRIMARY KEY (role_id, permission_key)
            );

            CREATE TABLE IF NOT EXISTS cms_admin_accounts (
                id BIGSERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                is_superuser BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_login_at TIMESTAMPTZ
            );
            ALTER TABLE cms_admin_accounts
                ADD COLUMN IF NOT EXISTS theme_preference TEXT NOT NULL DEFAULT 'system';
            ALTER TABLE cms_admin_accounts
                ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE cms_admin_accounts
                DROP CONSTRAINT IF EXISTS cms_admin_accounts_theme_preference_check;
            ALTER TABLE cms_admin_accounts
                ADD CONSTRAINT cms_admin_accounts_theme_preference_check
                CHECK (theme_preference IN ('dark', 'light', 'system'));
            CREATE INDEX IF NOT EXISTS idx_cms_admin_accounts_active
                ON cms_admin_accounts(is_active, username);
            CREATE INDEX IF NOT EXISTS idx_cms_admin_accounts_username_lower
                ON cms_admin_accounts((lower(username)), id);
            CREATE INDEX IF NOT EXISTS idx_cms_admin_accounts_display_name_lower
                ON cms_admin_accounts((lower(display_name)), id)
                WHERE display_name IS NOT NULL;

            CREATE TABLE IF NOT EXISTS cms_admin_roles (
                admin_id BIGINT NOT NULL REFERENCES cms_admin_accounts(id) ON DELETE CASCADE,
                role_id BIGINT NOT NULL REFERENCES cms_roles(id) ON DELETE CASCADE,
                PRIMARY KEY (admin_id, role_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cms_admin_roles_role_admin
                ON cms_admin_roles(role_id, admin_id);

            CREATE TABLE IF NOT EXISTS cms_sprint_plans (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                payload JSONB NOT NULL,
                created_by BIGINT REFERENCES cms_admin_accounts(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_cms_sprint_plans_updated
                ON cms_sprint_plans(updated_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS cms_retros (
                id BIGSERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'live', 'done')),
                config JSONB NOT NULL DEFAULT '{}'::jsonb,
                snapshot JSONB,
                ai_summary JSONB,
                created_by BIGINT REFERENCES cms_admin_accounts(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_cms_retros_updated
                ON cms_retros(updated_at DESC, id DESC);
            """
        )
        await self._ensure_team_schema(conn)

async def _ensure_team_schema(self, conn: asyncpg.Connection) -> None:
    """Team tables/columns are applied in a separate execute so upgrades from
    older deployments always run even if the main schema block was cached."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cms_teams (
            id BIGSERIAL PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_cms_teams_active_name
            ON cms_teams(is_active, lower(name), id);

        CREATE TABLE IF NOT EXISTS cms_admin_teams (
            admin_id BIGINT NOT NULL REFERENCES cms_admin_accounts(id) ON DELETE CASCADE,
            team_id BIGINT NOT NULL REFERENCES cms_teams(id) ON DELETE CASCADE,
            PRIMARY KEY (admin_id, team_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cms_admin_teams_team_admin
            ON cms_admin_teams(team_id, admin_id);
        CREATE INDEX IF NOT EXISTS idx_cms_admin_teams_admin_team
            ON cms_admin_teams(admin_id, team_id);

        ALTER TABLE cms_sessions
            ADD COLUMN IF NOT EXISTS team_id BIGINT REFERENCES cms_teams(id) ON DELETE SET NULL;
        ALTER TABLE cms_sprint_plans
            ADD COLUMN IF NOT EXISTS team_id BIGINT REFERENCES cms_teams(id) ON DELETE SET NULL;
        ALTER TABLE cms_retros
            ADD COLUMN IF NOT EXISTS team_id BIGINT REFERENCES cms_teams(id) ON DELETE SET NULL;

        CREATE TABLE IF NOT EXISTS cms_scope_boards (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            month TEXT NOT NULL,
            capacity_sp NUMERIC(10, 2) NOT NULL DEFAULT 0,
            plan_jql TEXT NOT NULL DEFAULT '',
            unplan_jql TEXT NOT NULL DEFAULT '',
            todo_jql TEXT NOT NULL DEFAULT '',
            test_jql TEXT NOT NULL DEFAULT '',
            scope_sections JSONB,
            snapshot JSONB,
            team_id BIGINT REFERENCES cms_teams(id) ON DELETE SET NULL,
            created_by BIGINT REFERENCES cms_admin_accounts(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS team_id BIGINT REFERENCES cms_teams(id) ON DELETE SET NULL;
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS todo_jql TEXT NOT NULL DEFAULT '';
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS test_jql TEXT NOT NULL DEFAULT '';
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS scope_sections JSONB;
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS ai_summary JSONB;
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS ai_summary_history JSONB NOT NULL DEFAULT '[]'::jsonb;
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS layout_order JSONB NOT NULL DEFAULT '[]'::jsonb;
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS flow_pace_chart_order JSONB NOT NULL DEFAULT '[]'::jsonb;
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS report_type TEXT NOT NULL DEFAULT 'monthly';
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS previous_release_jql TEXT NOT NULL DEFAULT '';
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS next_release_jql TEXT NOT NULL DEFAULT '';
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS custom_release_name TEXT NOT NULL DEFAULT '';
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS custom_release_jql TEXT NOT NULL DEFAULT '';
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS release_queries JSONB NOT NULL DEFAULT '[]'::jsonb;
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS release_comment TEXT NOT NULL DEFAULT '';
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS previous_release_comment TEXT NOT NULL DEFAULT '';
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS next_release_comment TEXT NOT NULL DEFAULT '';
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS custom_release_comment TEXT NOT NULL DEFAULT '';
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS workload_mode TEXT NOT NULL DEFAULT 'sp';
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS capacity_sp_dev NUMERIC(10, 2);
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS capacity_sp_test NUMERIC(10, 2);
        ALTER TABLE cms_scope_boards
            ADD COLUMN IF NOT EXISTS plan_epic_key TEXT NOT NULL DEFAULT '';
        CREATE INDEX IF NOT EXISTS idx_cms_scope_boards_updated
            ON cms_scope_boards(updated_at DESC, id DESC);

        CREATE INDEX IF NOT EXISTS idx_cms_sessions_team_updated
            ON cms_sessions(team_id, updated_at DESC, id DESC)
            WHERE deleted_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_cms_sprint_plans_team_updated
            ON cms_sprint_plans(team_id, updated_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_cms_retros_team_updated
            ON cms_retros(team_id, updated_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_cms_scope_boards_team_updated
            ON cms_scope_boards(team_id, updated_at DESC, id DESC);
        """
    )

async def ensure_access_defaults(self, bootstrap_username: str, bootstrap_password: str) -> None:
    async with self.pool.acquire() as conn:
        async with conn.transaction():
            for permission in CMS_PERMISSION_DEFINITIONS:
                await conn.execute(
                    """
                    INSERT INTO cms_permissions (key, label, description)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (key) DO UPDATE SET
                        label = EXCLUDED.label,
                        description = EXCLUDED.description
                    """,
                    permission["key"],
                    permission["label"],
                    permission["description"],
                )

            for page in CMS_PAGE_DEFINITIONS:
                await conn.execute(
                    """
                    INSERT INTO cms_pages (key, label, path, permission_key, sort_order)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (key) DO UPDATE SET
                        label = EXCLUDED.label,
                        path = EXCLUDED.path,
                        permission_key = EXCLUDED.permission_key,
                        sort_order = EXCLUDED.sort_order,
                        updated_at = NOW()
                    """,
                    page["key"],
                    page["label"],
                    page["path"],
                    page["permission_key"],
                    page["sort_order"],
                )

            if DEPRECATED_CMS_PAGE_KEYS:
                await conn.execute(
                    """
                    UPDATE cms_pages
                    SET is_enabled = FALSE, updated_at = NOW()
                    WHERE key = ANY($1::text[]) AND is_enabled = TRUE
                    """,
                    list(DEPRECATED_CMS_PAGE_KEYS),
                )

            superadmin_role_id = await self._upsert_system_role(
                conn,
                "superadmin",
                "Superadmin",
                "Full CMS access, including access management.",
                ALL_PERMISSION_KEYS,
            )
            await self._upsert_system_role(
                conn,
                "viewer",
                "Viewer",
                "Read-only access to operational CMS pages.",
                OPERATIONAL_VIEW_PERMISSIONS,
            )
            await self._upsert_system_role(
                conn,
                "access_manager",
                "Access manager",
                "Can view and manage CMS admins and roles.",
                [PERM_ACCESS_VIEW, PERM_ACCESS_MANAGE],
            )
            await self._upsert_system_role(
                conn,
                "session_manager",
                "Session manager",
                "Can facilitate planning sessions and manage active task queues.",
                [
                    PERM_SESSIONS_VIEW,
                    PERM_TASKS_MANAGE,
                    PERM_APP_SESSIONS_MANAGE,
                    PERM_PLANNER_VIEW,
                ],
            )

            if bootstrap_username and bootstrap_password:
                # One-time bootstrap: env credentials seed the first superadmin only.
                # Later restarts must not reset password, is_active, or is_superuser.
                admin_id = await conn.fetchval(
                    """
                    INSERT INTO cms_admin_accounts (
                        username, password_hash, display_name,
                        is_active, is_superuser, updated_at
                    )
                    VALUES ($1, $2, $3, TRUE, TRUE, NOW())
                    ON CONFLICT (username) DO NOTHING
                    RETURNING id
                    """,
                    bootstrap_username,
                    hash_password(bootstrap_password),
                    bootstrap_username,
                )
                if admin_id is not None:
                    await conn.execute(
                        """
                        INSERT INTO cms_admin_roles (admin_id, role_id)
                        VALUES ($1, $2)
                        ON CONFLICT DO NOTHING
                        """,
                        admin_id,
                        superadmin_role_id,
                    )

            team_count = await conn.fetchval("SELECT COUNT(*) FROM cms_teams")
            if team_count == 0:
                await conn.execute(
                    """
                    INSERT INTO cms_teams (slug, name, description, updated_at)
                    VALUES ('default', 'Default', 'Default team', NOW())
                    ON CONFLICT (slug) DO NOTHING
                    """
                )

async def _upsert_system_role(
    self,
    conn: asyncpg.Connection,
    key: str,
    name: str,
    description: str,
    permission_keys: list[str],
) -> int:
    role_id = await conn.fetchval(
        """
        INSERT INTO cms_roles (key, name, description, is_system, updated_at)
        VALUES ($1, $2, $3, TRUE, NOW())
        ON CONFLICT (key) DO UPDATE SET
            name = EXCLUDED.name,
            description = EXCLUDED.description,
            is_system = TRUE,
            updated_at = NOW()
        RETURNING id
        """,
        key,
        name,
        description,
    )
    await conn.execute("DELETE FROM cms_role_permissions WHERE role_id = $1", role_id)
    if permission_keys:
        await conn.executemany(
            """
            INSERT INTO cms_role_permissions (role_id, permission_key)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            [(role_id, permission_key) for permission_key in permission_keys],
        )
    return int(role_id)

async def close(self) -> None:
    await self.pool.close()

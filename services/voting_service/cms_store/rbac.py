"""CMS store mixin: admin auth, roles, and permissions."""

from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg

from services.voting_service.cms_rbac import hash_password, verify_password
from services.voting_service.cms_store._helpers import _row_to_dict, _team_row


class RBACMixin:
    """Admin accounts, RBAC roles, and permission helpers."""

    async def verify_admin_login(self, username: str, password: str) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, username, password_hash, is_active
                FROM cms_admin_accounts
                WHERE username = $1
                """,
                username,
            )
            password_hash = row["password_hash"] if row else ""
            password_ok = verify_password(password, password_hash)
            if not row or not row["is_active"] or not password_ok:
                return None
            await conn.execute(
                "UPDATE cms_admin_accounts SET last_login_at = NOW(), updated_at = NOW() WHERE id = $1",
                row["id"],
            )
        return await self.get_admin_principal(admin_id=int(row["id"]))

    async def get_admin_principal(
        self,
        admin_id: Optional[int] = None,
        username: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, username, display_name,
                       is_active, is_superuser, created_at, updated_at, last_login_at,
                       COALESCE(theme_preference, 'system') AS theme_preference,
                       COALESCE(token_version, 1) AS token_version
                FROM cms_admin_accounts
                WHERE ($1::bigint IS NOT NULL AND id = $1)
                   OR ($2::text IS NOT NULL AND username = $2)
                """,
                admin_id,
                username,
            )
            if not row or not row["is_active"]:
                return None

            role_rows = await conn.fetch(
                """
                SELECT r.id, r.key, r.name, r.description, r.is_system
                FROM cms_roles r
                JOIN cms_admin_roles ar ON ar.role_id = r.id
                WHERE ar.admin_id = $1
                ORDER BY r.name ASC, r.id ASC
                """,
                row["id"],
            )

            if row["is_superuser"]:
                permission_rows = await conn.fetch("SELECT key FROM cms_permissions ORDER BY key ASC")
            else:
                permission_rows = await conn.fetch(
                    """
                    SELECT DISTINCT p.key
                    FROM cms_permissions p
                    JOIN cms_role_permissions rp ON rp.permission_key = p.key
                    JOIN cms_admin_roles ar ON ar.role_id = rp.role_id
                    WHERE ar.admin_id = $1
                    ORDER BY p.key ASC
                    """,
                    row["id"],
                )

            permission_keys = [item["key"] for item in permission_rows]
            page_rows = await conn.fetch(
                """
                SELECT key, label, path, permission_key, sort_order
                FROM cms_pages
                WHERE is_enabled
                  AND ($1::boolean OR permission_key = ANY($2::text[]))
                ORDER BY sort_order ASC, key ASC
                """,
                row["is_superuser"],
                permission_keys,
            )

            team_rows = await conn.fetch(
                """
                SELECT t.id, t.slug, t.name, t.description, t.is_active,
                       t.created_at, t.updated_at
                FROM cms_teams t
                JOIN cms_admin_teams at ON at.team_id = t.id
                WHERE at.admin_id = $1 AND t.is_active
                ORDER BY lower(t.name) ASC, t.id ASC
                """,
                row["id"],
            )

        data = _row_to_dict(row)
        data["roles"] = [_row_to_dict(role) for role in role_rows]
        data["permissions"] = permission_keys
        data["pages"] = [_row_to_dict(page) for page in page_rows]
        data["teams"] = [_team_row(team) for team in team_rows]
        data["team_ids"] = [int(team["id"]) for team in team_rows]
        return data

    async def update_admin_theme_preference(self, admin_id: int, theme_preference: str) -> bool:
        """Persist the admin's theme choice. Returns True if the account exists and is active."""
        if theme_preference not in ("dark", "light", "system"):
            raise ValueError(f"invalid theme_preference: {theme_preference!r}")
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE cms_admin_accounts
                SET theme_preference = $2,
                    updated_at = NOW()
                WHERE id = $1 AND is_active
                """,
                admin_id,
                theme_preference,
            )
        try:
            affected = int(result.split()[-1])
        except (ValueError, IndexError):
            affected = 0
        return affected > 0

    async def list_cms_permissions(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, label, description
                FROM cms_permissions
                ORDER BY key ASC
                """
            )
        return [_row_to_dict(row) for row in rows]

    async def list_cms_pages(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, label, path, permission_key, sort_order, is_enabled
                FROM cms_pages
                ORDER BY sort_order ASC, key ASC
                """
            )
        return [_row_to_dict(row) for row in rows]

    async def list_cms_roles(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, key, name, description, is_system, created_at, updated_at
                FROM cms_roles
                ORDER BY is_system DESC, name ASC, id ASC
                """
            )
            permissions = await conn.fetch(
                """
                SELECT role_id, permission_key
                FROM cms_role_permissions
                ORDER BY permission_key ASC
                """
            )
        permission_map: dict[int, list[str]] = {}
        for permission in permissions:
            permission_map.setdefault(int(permission["role_id"]), []).append(permission["permission_key"])
        roles = []
        for row in rows:
            item = _row_to_dict(row)
            item["permission_keys"] = permission_map.get(int(row["id"]), [])
            roles.append(item)
        return roles

    async def create_cms_role(
        self,
        key: str,
        name: str,
        description: str,
        permission_keys: list[str],
    ) -> dict[str, Any]:
        clean_permissions = await self._valid_permission_keys(permission_keys)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                role_id = await conn.fetchval(
                    """
                    INSERT INTO cms_roles (key, name, description, is_system, updated_at)
                    VALUES ($1, $2, $3, FALSE, NOW())
                    RETURNING id
                    """,
                    key,
                    name,
                    description,
                )
                await self._replace_role_permissions(conn, int(role_id), clean_permissions)
        return await self.get_cms_role(int(role_id))

    async def update_cms_role(
        self,
        role_id: int,
        name: str,
        description: str,
        permission_keys: list[str],
    ) -> Optional[dict[str, Any]]:
        clean_permissions = await self._valid_permission_keys(permission_keys)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.fetchrow(
                    """
                    UPDATE cms_roles
                    SET name = $2, description = $3, updated_at = NOW()
                    WHERE id = $1 AND is_system = FALSE
                    RETURNING id
                    """,
                    role_id,
                    name,
                    description,
                )
                if not updated:
                    return None
                await self._replace_role_permissions(conn, role_id, clean_permissions)
        return await self.get_cms_role(role_id)

    async def get_cms_role(self, role_id: int) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, key, name, description, is_system, created_at, updated_at
                FROM cms_roles
                WHERE id = $1
                """,
                role_id,
            )
            permission_rows = await conn.fetch(
                """
                SELECT permission_key
                FROM cms_role_permissions
                WHERE role_id = $1
                ORDER BY permission_key ASC
                """,
                role_id,
            )
        item = _row_to_dict(row)
        item["permission_keys"] = [permission["permission_key"] for permission in permission_rows]
        return item

    async def _load_admin_teams(self, conn: asyncpg.Connection, admin_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not admin_ids:
            return {}
        rows = await conn.fetch(
            """
            SELECT at.admin_id, t.id, t.slug, t.name, t.description, t.is_active,
                   t.created_at, t.updated_at
            FROM cms_admin_teams at
            JOIN cms_teams t ON t.id = at.team_id
            WHERE at.admin_id = ANY($1::bigint[])
            ORDER BY lower(t.name) ASC, t.id ASC
            """,
            admin_ids,
        )
        team_map: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            team_map.setdefault(int(row["admin_id"]), []).append(_team_row(row))
        return team_map

    async def _replace_admin_teams(
        self,
        conn: asyncpg.Connection,
        admin_id: int,
        team_ids: list[int],
    ) -> None:
        await conn.execute("DELETE FROM cms_admin_teams WHERE admin_id = $1", admin_id)
        if team_ids:
            await conn.executemany(
                """
                INSERT INTO cms_admin_teams (admin_id, team_id)
                SELECT $1, id
                FROM cms_teams
                WHERE id = $2 AND is_active
                ON CONFLICT DO NOTHING
                """,
                [(admin_id, team_id) for team_id in sorted(set(team_ids))],
            )

    async def list_cms_admins(
        self,
        limit: int,
        cursor: Optional[str] = None,
        q: Optional[str] = None,
        active: Optional[bool] = None,
        role_id: Optional[int] = None,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)
        cur = decode_cursor(cursor)
        cursor_username = cur.get("username")
        cursor_id = cur.get("id")
        clean_q = q.strip().lower() if q and q.strip() else None
        pattern = f"{clean_q}%" if clean_q else None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, username, display_name, is_active,
                       is_superuser, created_at, updated_at, last_login_at
                FROM cms_admin_accounts
                WHERE (
                    $1::text IS NULL
                    OR lower(username) LIKE $1
                    OR lower(COALESCE(display_name, '')) LIKE $1
                )
                  AND ($2::boolean IS NULL OR is_active = $2)
                  AND (
                    $3::bigint IS NULL
                    OR EXISTS (
                        SELECT 1
                        FROM cms_admin_roles role_filter
                        WHERE role_filter.admin_id = cms_admin_accounts.id
                          AND role_filter.role_id = $3
                    )
                  )
                  AND (
                      $4::text IS NULL
                      OR (lower(username), id) > ($4::text, $5::bigint)
                  )
                ORDER BY lower(username) ASC, id ASC
                LIMIT $6
                """,
                pattern,
                active,
                role_id,
                cursor_username,
                cursor_id,
                limit + 1,
            )
            page_rows = rows[:limit]
            admin_ids = [int(row["id"]) for row in page_rows]
            roles = []
            team_map: dict[int, list[dict[str, Any]]] = {}
            if admin_ids:
                roles = await conn.fetch(
                    """
                    SELECT ar.admin_id, r.id, r.key, r.name, r.is_system
                    FROM cms_admin_roles ar
                    JOIN cms_roles r ON r.id = ar.role_id
                    WHERE ar.admin_id = ANY($1::bigint[])
                    ORDER BY r.name ASC, r.id ASC
                    """,
                    admin_ids,
                )
                team_map = await self._load_admin_teams(conn, admin_ids)
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = encode_cursor({"username": last["username"].lower(), "id": int(last["id"])})
        role_map: dict[int, list[dict[str, Any]]] = {}
        for role in roles:
            role_map.setdefault(int(role["admin_id"]), []).append(
                {
                    "id": int(role["id"]),
                    "key": role["key"],
                    "name": role["name"],
                    "is_system": role["is_system"],
                }
            )
        admins = []
        for row in page_rows:
            item = _row_to_dict(row)
            admin_id = int(row["id"])
            teams = team_map.get(admin_id, [])
            item["roles"] = role_map.get(admin_id, [])
            item["teams"] = teams
            item["team_ids"] = [int(team["id"]) for team in teams]
            admins.append(item)
        return {"items": admins, "next_cursor": next_cursor, "limit": limit}

    async def list_all_cms_admins(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, username, display_name, is_active,
                       is_superuser, created_at, updated_at, last_login_at
                FROM cms_admin_accounts
                ORDER BY lower(username) ASC, id ASC
                """
            )
            roles = await conn.fetch(
                """
                SELECT ar.admin_id, r.id, r.key, r.name, r.is_system
                FROM cms_admin_roles ar
                JOIN cms_roles r ON r.id = ar.role_id
                ORDER BY r.name ASC, r.id ASC
                """
            )
            team_map = await self._load_admin_teams(conn, [int(row["id"]) for row in rows])
        role_map: dict[int, list[dict[str, Any]]] = {}
        for role in roles:
            role_map.setdefault(int(role["admin_id"]), []).append(
                {
                    "id": int(role["id"]),
                    "key": role["key"],
                    "name": role["name"],
                    "is_system": role["is_system"],
                }
            )
        admins = []
        for row in rows:
            item = _row_to_dict(row)
            admin_id = int(row["id"])
            teams = team_map.get(admin_id, [])
            item["roles"] = role_map.get(admin_id, [])
            item["teams"] = teams
            item["team_ids"] = [int(team["id"]) for team in teams]
            admins.append(item)
        return admins

    async def get_cms_admin_account(self, admin_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, username, display_name, is_active,
                       is_superuser, created_at, updated_at, last_login_at
                FROM cms_admin_accounts
                WHERE id = $1
                """,
                admin_id,
            )
            if not row:
                return None
            roles = await conn.fetch(
                """
                SELECT r.id, r.key, r.name, r.is_system
                FROM cms_admin_roles ar
                JOIN cms_roles r ON r.id = ar.role_id
                WHERE ar.admin_id = $1
                ORDER BY r.name ASC, r.id ASC
                """,
                admin_id,
            )
            team_map = await self._load_admin_teams(conn, [admin_id])
        item = _row_to_dict(row)
        teams = team_map.get(admin_id, [])
        item["roles"] = [
            {
                "id": int(role["id"]),
                "key": role["key"],
                "name": role["name"],
                "is_system": role["is_system"],
            }
            for role in roles
        ]
        item["teams"] = teams
        item["team_ids"] = [int(team["id"]) for team in teams]
        return item

    async def create_cms_admin(
        self,
        username: str,
        password: str,
        display_name: Optional[str],
        is_active: bool,
        role_ids: list[int],
        team_ids: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                admin_id = await conn.fetchval(
                    """
                    INSERT INTO cms_admin_accounts (
                        username, password_hash, display_name, is_active, is_superuser, updated_at
                    )
                    VALUES ($1, $2, $3, $4, FALSE, NOW())
                    RETURNING id
                    """,
                    username,
                    hash_password(password),
                    display_name,
                    is_active,
                )
                await self._replace_admin_roles(conn, int(admin_id), role_ids)
                await self._replace_admin_teams(conn, int(admin_id), team_ids or [])
        return await self.get_cms_admin_account(int(admin_id)) or {}

    async def update_cms_admin(
        self,
        admin_id: int,
        display_name: Optional[str],
        is_active: bool,
        role_ids: list[int],
        password: Optional[str] = None,
        team_ids: Optional[list[int]] = None,
        *,
        update_teams: bool = False,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if password:
                    row = await conn.fetchrow(
                        """
                        UPDATE cms_admin_accounts
                        SET display_name = $2,
                            is_active = $3,
                            password_hash = $4,
                            token_version = token_version + 1,
                            updated_at = NOW()
                        WHERE id = $1
                        RETURNING id
                        """,
                        admin_id,
                        display_name,
                        is_active,
                        hash_password(password),
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        UPDATE cms_admin_accounts
                        SET display_name = $2,
                            is_active = $3,
                            updated_at = NOW()
                        WHERE id = $1
                        RETURNING id
                        """,
                        admin_id,
                        display_name,
                        is_active,
                    )
                if not row:
                    return None
                await self._replace_admin_roles(conn, admin_id, role_ids)
                if update_teams:
                    await self._replace_admin_teams(conn, admin_id, team_ids or [])
        return await self.get_cms_admin_account(admin_id)

    async def _valid_permission_keys(self, permission_keys: list[str]) -> list[str]:
        if not permission_keys:
            return []
        unique_keys = sorted(set(permission_keys))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key
                FROM cms_permissions
                WHERE key = ANY($1::text[])
                ORDER BY key ASC
                """,
                unique_keys,
            )
        return [row["key"] for row in rows]

    async def _replace_role_permissions(
        self,
        conn: asyncpg.Connection,
        role_id: int,
        permission_keys: list[str],
    ) -> None:
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

    async def _replace_admin_roles(
        self,
        conn: asyncpg.Connection,
        admin_id: int,
        role_ids: list[int],
    ) -> None:
        await conn.execute("DELETE FROM cms_admin_roles WHERE admin_id = $1", admin_id)
        if role_ids:
            await conn.executemany(
                """
                INSERT INTO cms_admin_roles (admin_id, role_id)
                SELECT $1, id
                FROM cms_roles
                WHERE id = $2
                ON CONFLICT DO NOTHING
                """,
                [(admin_id, role_id) for role_id in sorted(set(role_ids))],
            )


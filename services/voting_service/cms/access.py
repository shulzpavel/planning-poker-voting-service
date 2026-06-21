"""CMS RBAC access management endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from services.voting_service.cms_rbac import PERM_ACCESS_MANAGE, PERM_ACCESS_VIEW
from services.voting_service.cms_store import DEFAULT_LIMIT, MAX_LIMIT
from services.voting_service._http_shared import (
    CmsPrincipal,
    _audit,
    _get_cms_store,
    require_permission,
)

router = APIRouter()


class RoleCreateRequest(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)
    permission_keys: list[str] = Field(default_factory=list)


class RoleUpdateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)
    permission_keys: list[str] = Field(default_factory=list)

class AdminCreateRequest(BaseModel):
    username: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=8, max_length=256)
    display_name: Optional[str] = Field(default=None, max_length=120)
    is_active: bool = True
    role_ids: list[int] = Field(default_factory=list)
    team_ids: list[int] = Field(default_factory=list)


class AdminUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=120)
    is_active: bool = True
    role_ids: list[int] = Field(default_factory=list)
    team_ids: list[int] = Field(default_factory=list)
    password: Optional[str] = Field(default=None, min_length=8, max_length=256)

@router.get("/cms/access/permissions")
async def cms_access_permissions(
    request: Request,
    _: CmsPrincipal = Depends(require_permission(PERM_ACCESS_VIEW)),
) -> dict:
    return {"items": await _get_cms_store(request).list_cms_permissions()}


@router.get("/cms/access/pages")
async def cms_access_pages(
    request: Request,
    _: CmsPrincipal = Depends(require_permission(PERM_ACCESS_VIEW)),
) -> dict:
    return {"items": await _get_cms_store(request).list_cms_pages()}


@router.get("/cms/access/roles")
async def cms_access_roles(
    request: Request,
    _: CmsPrincipal = Depends(require_permission(PERM_ACCESS_VIEW)),
) -> dict:
    return {"items": await _get_cms_store(request).list_cms_roles()}


@router.post("/cms/access/roles")
async def cms_access_create_role(
    body: RoleCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_ACCESS_MANAGE)),
) -> dict:
    try:
        role = await _get_cms_store(request).create_cms_role(
            key=body.key,
            name=body.name,
            description=body.description,
            permission_keys=body.permission_keys,
        )
    except Exception as exc:
        await _audit(request, "cms.access.role.create", actor.username, "failed", {"error": str(exc)})
        raise HTTPException(status_code=400, detail="Role could not be created") from exc
    await _audit(request, "cms.access.role.create", actor.username, "ok", {"role_id": role["id"]})
    return role


@router.patch("/cms/access/roles/{role_id}")
async def cms_access_update_role(
    role_id: int,
    body: RoleUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_ACCESS_MANAGE)),
) -> dict:
    role = await _get_cms_store(request).update_cms_role(
        role_id=role_id,
        name=body.name,
        description=body.description,
        permission_keys=body.permission_keys,
    )
    if not role:
        raise HTTPException(status_code=404, detail="Role not found or system role is read-only")
    await _audit(request, "cms.access.role.update", actor.username, "ok", {"role_id": role_id})
    return role


@router.get("/cms/access/admins")
async def cms_access_admins(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    active: Optional[bool] = None,
    role_id: Optional[int] = None,
    _: CmsPrincipal = Depends(require_permission(PERM_ACCESS_VIEW)),
) -> dict:
    return await _get_cms_store(request).list_cms_admins(
        limit=limit,
        cursor=cursor,
        q=q,
        active=active,
        role_id=role_id,
    )


@router.post("/cms/access/admins")
async def cms_access_create_admin(
    body: AdminCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_ACCESS_MANAGE)),
) -> dict:
    try:
        if not actor.is_superuser and body.team_ids:
            raise HTTPException(status_code=403, detail="Forbidden")
        admin = await _get_cms_store(request).create_cms_admin(
            username=body.username,
            password=body.password,
            display_name=body.display_name,
            is_active=body.is_active,
            role_ids=body.role_ids,
            team_ids=body.team_ids if actor.is_superuser else [],
        )
    except Exception as exc:
        await _audit(request, "cms.access.admin.create", actor.username, "failed", {"error": str(exc)})
        raise HTTPException(status_code=400, detail="Admin could not be created") from exc
    await _audit(request, "cms.access.admin.create", actor.username, "ok", {"admin_id": admin["id"]})
    return admin


@router.patch("/cms/access/admins/{admin_id}")
async def cms_access_update_admin(
    admin_id: int,
    body: AdminUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_ACCESS_MANAGE)),
) -> dict:
    if admin_id == actor.id and not body.is_active:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own admin account")
    if not actor.is_superuser and body.team_ids:
        raise HTTPException(status_code=403, detail="Forbidden")
    admin = await _get_cms_store(request).update_cms_admin(
        admin_id=admin_id,
        display_name=body.display_name,
        is_active=body.is_active,
        role_ids=body.role_ids,
        password=body.password,
        team_ids=body.team_ids if actor.is_superuser else None,
        update_teams=actor.is_superuser,
    )
    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found")
    await _audit(request, "cms.access.admin.update", actor.username, "ok", {"admin_id": admin_id})
    return admin


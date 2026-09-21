"""User management API + page (admin only)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, field_validator

from ..auth.routes import require_admin, require_admin_page
from ..auth.service import AuthError
from .. import templates
from .service import ROLES, STATUSES, UserService

router = APIRouter(prefix="/api/users", tags=["users"])
page = APIRouter(tags=["users-pages"])


def get_service(request: Request) -> UserService:
    return request.app.state.user_service


def _raise(exc: AuthError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.message)


class UserIn(BaseModel):
    name: str
    email: EmailStr
    password: str | None = None
    role: str = "USER"
    status: str = "ACTIVE"

    @field_validator("password")
    @classmethod
    def _pw(cls, v: str | None) -> str | None:
        if v in ("", None):
            return None
        return v


@router.get("")
async def api_list_users(
    service: UserService = Depends(get_service), user: dict = Depends(require_admin)
):
    return {"users": await service.list()}


@router.post("")
async def api_create_user(
    payload: UserIn,
    request: Request,
    actor: dict = Depends(require_admin),
    service: UserService = Depends(get_service),
):
    if not payload.password:
        raise HTTPException(status_code=400, detail="Password is required for new users.")
    try:
        user = await service.create(
            name=payload.name,
            email=str(payload.email),
            password=payload.password,
            role=payload.role,
            status=payload.status,
            actor=actor,
        )
    except AuthError as exc:
        _raise(exc)
    return {"user": user}


@router.get("/{user_id}")
async def api_get_user(
    user_id: int,
    service: UserService = Depends(get_service),
    actor: dict = Depends(require_admin),
):
    users = await service.list()
    for u in users:
        if u["id"] == user_id:
            return {"user": u}
    raise HTTPException(status_code=404, detail="User not found.")


@router.put("/{user_id}")
async def api_update_user(
    user_id: int,
    payload: UserIn,
    request: Request,
    actor: dict = Depends(require_admin),
    service: UserService = Depends(get_service),
):
    fields = {
        "name": payload.name,
        "email": str(payload.email),
        "role": payload.role,
        "status": payload.status,
    }
    try:
        user = await service.update(user_id, actor=actor, **fields)
    except AuthError as exc:
        _raise(exc)
    return {"user": user}


@router.delete("/{user_id}")
async def api_delete_user(
    user_id: int,
    request: Request,
    actor: dict = Depends(require_admin),
    service: UserService = Depends(get_service),
):
    try:
        await service.delete(user_id, actor=actor)
    except AuthError as exc:
        _raise(exc)
    return {"ok": True}


class PasswordIn(BaseModel):
    password: str


@router.post("/{user_id}/reset-password")
async def api_reset_password(
    user_id: int,
    payload: PasswordIn,
    request: Request,
    actor: dict = Depends(require_admin),
    service: UserService = Depends(get_service),
):
    if not payload.password:
        raise HTTPException(status_code=400, detail="New password is required.")
    try:
        user = await service.reset_password(user_id, payload.password, actor=actor)
    except AuthError as exc:
        _raise(exc)
    return {"user": user}


@page.get("/users", include_in_schema=False)
async def users_page(request: Request, user: dict = Depends(require_admin_page)):
    users = await request.app.state.user_service.list()
    return templates.render(request, "users.html", {"users": users})

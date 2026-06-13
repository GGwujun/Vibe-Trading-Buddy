"""Authentication HTTP routes: register / login / disclaimer / me.

These endpoints are PUBLIC (no require_auth) except /auth/disclaimer/accept
and /auth/me, which validate the JWT via ``require_user``.

Mounted by ``agent/api_server.py`` via ``register_auth_routes(app)``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.auth.jwt_utils import create_token, user_id_from_token
from src.auth.models import AuthResponse, LoginRequest, RegisterRequest, User
from src.auth.store import UserStore

logger = logging.getLogger(__name__)

# Module-level singleton store (SQLite connection is thread-safe with the
# store's internal lock).
_store: UserStore | None = None


def _get_store() -> UserStore:
    global _store
    if _store is None:
        _store = UserStore()
    return _store


_security = HTTPBearer(auto_error=False)


async def require_user(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(_security),
) -> dict[str, Any]:
    """Validate the JWT bearer token and return the user dict.

    Used by user-gated endpoints (/auth/me, /auth/disclaimer/accept).
    """
    token = cred.credentials if cred and cred.credentials else ""
    user_id = user_id_from_token(token)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或登录已过期")
    user = _get_store().get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    return user


async def require_admin(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(_security),
) -> dict[str, Any]:
    """Validate JWT AND require the user to be an admin. Returns the user dict.

    Used by operator-only endpoints (system settings, etc.).
    """
    user = await require_user(request, cred)
    if not user.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def register_auth_routes(app: FastAPI) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/register", response_model=AuthResponse)
    async def register(body: RegisterRequest) -> AuthResponse:
        """Register a new user. ``agree`` must be true (disclaimer checkbox)."""
        if not body.agree:
            raise HTTPException(status_code=400, detail="必须同意免责声明才能注册")
        try:
            user = _get_store().create_user(body.email, body.password)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        token = create_token(user["id"], user["email"])
        return AuthResponse(token=token, user=User(**user))

    @router.post("/login", response_model=AuthResponse)
    async def login(body: LoginRequest) -> AuthResponse:
        """Login with email + password. Returns a JWT + the user."""
        user = _get_store().verify_credentials(body.email, body.password)
        if user is None:
            raise HTTPException(status_code=401, detail="邮箱或密码错误")
        token = create_token(user["id"], user["email"])
        return AuthResponse(token=token, user=User(**user))

    @router.get("/me", response_model=User)
    async def me(user: dict = Depends(require_user)) -> User:
        """Return the current user (validates the token)."""
        return User(**user)

    @router.post("/disclaimer/accept", response_model=User)
    async def accept_disclaimer(user: dict = Depends(require_user)) -> User:
        """Record that the user accepted the disclaimer."""
        _get_store().set_disclaimer_accepted(user["id"])
        updated = _get_store().get_by_id(user["id"]) or user
        return User(**updated)

    app.include_router(router)
    logger.info("Auth routes registered")
    return router

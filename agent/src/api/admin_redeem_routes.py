"""Admin redeem code management routes.

All endpoints require admin role (Depends(require_admin)).

Routes:
- GET  /admin/redeem-codes           — list all codes with optional status filter
- POST /admin/redeem-codes/generate  — generate new codes
- GET  /admin/redeem-codes/stats     — statistics (total, unused, used, expired)
"""

from __future__ import annotations

import logging
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.auth_routes import require_admin
from src.auth.store import UserStore
from src.credits.store import CreditStore

logger = logging.getLogger(__name__)

_credits_store: CreditStore | None = None
_user_store: UserStore | None = None


def _get_credits() -> CreditStore:
    global _credits_store
    if _credits_store is None:
        _credits_store = CreditStore()
    return _credits_store


def _get_users() -> UserStore:
    global _user_store
    if _user_store is None:
        _user_store = UserStore()
    return _user_store


def _get_user_email(user_id: str | None) -> str | None:
    """Get user email by user_id."""
    if not user_id:
        return None
    try:
        row = _get_users()._get_conn().execute(
            "SELECT email FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row["email"] if row else None
    except Exception:
        return None


def _gen_code(prefix: str, length: int = 8) -> str:
    """Generate a random code like PREFIX-XXXX-XXXX."""
    alphabet = string.ascii_uppercase + string.digits
    chunk = "".join(random.choices(alphabet, k=length // 2))
    chunk2 = "".join(random.choices(alphabet, k=length // 2))
    return f"{prefix}-{chunk}-{chunk2}"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class RedeemCodeItem(BaseModel):
    code: str
    credits: int
    status: str  # unused, used, expired
    redeemed_by: str | None = None  # user email
    redeemed_at: str | None = None
    created_at: str
    expires_at: str | None = None


class RedeemCodesListResponse(BaseModel):
    items: list[RedeemCodeItem]
    total: int
    unused: int
    used: int
    expired: int


class GenerateCodesRequest(BaseModel):
    credits: int = Field(..., ge=1, le=10000, description="每个兑换码的积分数量")
    count: int = Field(10, ge=1, le=100, description="生成数量")
    prefix: str = Field("SIGMX", max_length=10, description="兑换码前缀")
    days: int = Field(90, ge=0, le=365, description="有效天数（0=永久）")


class GenerateCodesResponse(BaseModel):
    codes: list[RedeemCodeItem]
    count: int
    credits: int
    expires_at: str | None


class RedeemCodesStats(BaseModel):
    total: int
    unused: int
    used: int
    expired: int
    total_credits_unused: int
    total_credits_used: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register_admin_redeem_routes(app: FastAPI) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin", "redeem-codes"])

    @router.get("/redeem-codes", response_model=RedeemCodesListResponse)
    async def list_codes(
        status: str = Query("all", description="筛选状态: all, unused, used, expired"),
        limit: int = Query(100, ge=1, le=500),
        _: dict = Depends(require_admin),
    ) -> RedeemCodesListResponse:
        """List all redeem codes with optional status filter."""
        store = _get_credits()
        all_codes = store.list_codes()
        now = datetime.now(timezone.utc)

        # Determine status for each code
        items = []
        unused_count = 0
        used_count = 0
        expired_count = 0

        for row in all_codes:
            # Determine actual status
            actual_status = row["status"]
            expires_at = row.get("expires_at")
            if actual_status == "unused" and expires_at:
                try:
                    if datetime.fromisoformat(expires_at) < now:
                        actual_status = "expired"
                except ValueError:
                    pass

            # Count by status
            if actual_status == "unused":
                unused_count += 1
            elif actual_status == "used":
                used_count += 1
            elif actual_status == "expired":
                expired_count += 1

            # Filter by requested status
            if status != "all" and actual_status != status:
                continue

            items.append(RedeemCodeItem(
                code=row["code"],
                credits=row["credits"],
                status=actual_status,
                redeemed_by=_get_user_email(row.get("redeemed_by")),
                redeemed_at=row.get("redeemed_at"),
                created_at=row["created_at"],
                expires_at=expires_at,
            ))

        # Apply limit
        items = items[:limit]

        return RedeemCodesListResponse(
            items=items,
            total=len(all_codes),
            unused=unused_count,
            used=used_count,
            expired=expired_count,
        )

    @router.post("/redeem-codes/generate", response_model=GenerateCodesResponse)
    async def generate_codes(
        body: GenerateCodesRequest,
        _: dict = Depends(require_admin),
    ) -> GenerateCodesResponse:
        """Generate new redeem codes."""
        store = _get_credits()
        expires_at = None
        if body.days > 0:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=body.days)).isoformat()

        codes: list[RedeemCodeItem] = []
        seen: set[str] = set()
        attempts = 0
        while len(codes) < body.count and attempts < body.count * 5:
            attempts += 1
            code = _gen_code(body.prefix)
            if code in seen:
                continue
            seen.add(code)
            try:
                store.create_redeem_code(code, body.credits, expires_at)
                codes.append(RedeemCodeItem(
                    code=code,
                    credits=body.credits,
                    status="unused",
                    redeemed_by=None,
                    redeemed_at=None,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    expires_at=expires_at,
                ))
            except Exception as exc:
                logger.warning("Failed to create code %s: %s", code, exc)

        if not codes:
            raise HTTPException(status_code=500, detail="生成兑换码失败")

        logger.info("Admin generated %d redeem codes, %d credits each, expires=%s",
                    len(codes), body.credits, expires_at or "永久")

        return GenerateCodesResponse(
            codes=codes,
            count=len(codes),
            credits=body.credits,
            expires_at=expires_at,
        )

    @router.get("/redeem-codes/stats", response_model=RedeemCodesStats)
    async def get_stats(_: dict = Depends(require_admin)) -> RedeemCodesStats:
        """Get redeem code statistics."""
        store = _get_credits()
        all_codes = store.list_codes()
        now = datetime.now(timezone.utc)

        total = len(all_codes)
        unused = 0
        used = 0
        expired = 0
        total_credits_unused = 0
        total_credits_used = 0

        for row in all_codes:
            status = row["status"]
            expires_at = row.get("expires_at")
            credits = row["credits"]

            # Determine actual status
            if status == "unused" and expires_at:
                try:
                    if datetime.fromisoformat(expires_at) < now:
                        status = "expired"
                except ValueError:
                    pass

            if status == "unused":
                unused += 1
                total_credits_unused += credits
            elif status == "used":
                used += 1
                total_credits_used += credits
            elif status == "expired":
                expired += 1

        return RedeemCodesStats(
            total=total,
            unused=unused,
            used=used,
            expired=expired,
            total_credits_unused=total_credits_unused,
            total_credits_used=total_credits_used,
        )

    app.include_router(router)
    logger.info("Admin redeem code routes registered")
    return router
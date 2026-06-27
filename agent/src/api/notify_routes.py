"""Notification config + test HTTP routes.

Routes:
- GET  /notify/config   — read the full notify config
- PUT  /notify/config   — save the full notify config
- POST /notify/test     — push a real market summary to one platform's webhook

Config is system-level (stored in ~/.vibe-trading/notify_config.json), not
per-user, so these endpoints use the existing require_auth (operator-level),
not require_user.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request

from src.notify.models import NotifyConfig, TestRequest, TestResponse
from src.notify.scheduler import notify_scheduler_enabled, run_notify_scheduler_loop
from src.notify.sender import send
from src.notify.store import load_config, save_config
from src.notify.summary import build_summary

logger = logging.getLogger(__name__)


def register_notify_routes(
    app: FastAPI,
    require_auth,
    require_event_stream_auth=None,
) -> APIRouter:
    router = APIRouter(prefix="/notify", tags=["notify"])

    @router.get("/config")
    async def get_config(request: Request, _=Depends(require_auth)) -> NotifyConfig:
        return load_config()

    @router.put("/config")
    async def put_config(cfg: NotifyConfig, request: Request, _=Depends(require_auth)) -> NotifyConfig:
        save_config(cfg)
        return cfg

    @router.post("/test", response_model=TestResponse)
    async def test_notify(body: TestRequest, request: Request, _=Depends(require_auth)) -> TestResponse:
        cfg = load_config()
        platform_cfg = getattr(cfg, body.platform, None) if body.platform in ("feishu", "dingtalk", "wechat") else None
        if platform_cfg is None:
            raise HTTPException(status_code=400, detail=f"未知平台: {body.platform}")
        if not platform_cfg.webhook_url:
            raise HTTPException(status_code=400, detail="该平台未配置 Webhook")
        title, markdown = build_summary()
        ok, message = send(body.platform, platform_cfg, title, markdown)
        return TestResponse(ok=ok, message=message)

    app.include_router(router)

    @app.on_event("startup")
    async def start_notify_scheduler() -> None:
        if not notify_scheduler_enabled():
            return
        app.state.notify_scheduler_task = asyncio.create_task(run_notify_scheduler_loop())

    logger.info("Notify routes registered")
    return router

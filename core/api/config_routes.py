"""
配置 API：GET/PUT /api/config；配置页 GET /config。
"""

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from core.api.chat_handler import ChatHandler
from core.config.repository import ConfigRepository
from core.plugin.base import PluginRegistry

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_config_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/types")
    def get_types() -> list[str]:
        """返回已注册的 type 列表，供配置页 type 下拉使用。"""
        return PluginRegistry.all_types()

    @router.get("/api/config")
    def get_config(request: Request) -> list[dict[str, Any]]:
        """获取配置（代理组 + 账号 name/type/auth）。"""
        repo: ConfigRepository | None = getattr(request.app.state, "config_repo", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="服务未就绪")
        return repo.load_raw()

    @router.put("/api/config")
    async def put_config(
        request: Request, config: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """更新配置并立即生效。"""
        repo: ConfigRepository | None = getattr(request.app.state, "config_repo", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="服务未就绪")
        if not config:
            raise HTTPException(status_code=400, detail="配置不能为空")
        for i, g in enumerate(config):
            if not isinstance(g, dict):
                raise HTTPException(status_code=400, detail=f"第 {i + 1} 项应为对象")
            if "fingerprint_id" not in g:
                raise HTTPException(
                    status_code=400, detail=f"代理组 {i + 1} 缺少字段: fingerprint_id"
                )
            use_proxy = g.get("use_proxy", True)
            if isinstance(use_proxy, str):
                use_proxy = use_proxy.strip().lower() not in {
                    "0",
                    "false",
                    "no",
                    "off",
                }
            else:
                use_proxy = bool(use_proxy)
            if use_proxy and not str(g.get("proxy_host", "")).strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"代理组 {i + 1} 启用了代理，需填写 proxy_host",
                )
            accounts = g.get("accounts", [])
            if not accounts:
                raise HTTPException(
                    status_code=400, detail=f"代理组 {i + 1} 至少需要一个账号"
                )
            for j, a in enumerate(accounts):
                if not isinstance(a, dict) or not (a.get("name") or "").strip():
                    raise HTTPException(
                        status_code=400,
                        detail=f"代理组 {i + 1} 账号 {j + 1} 需包含 name",
                    )
                if not (a.get("type") or "").strip():
                    raise HTTPException(
                        status_code=400,
                        detail=f"代理组 {i + 1} 账号 {j + 1} 需包含 type（如 claude）",
                    )
        try:
            repo.save_raw(config)
        except Exception as e:
            logger.exception("保存配置失败")
            raise HTTPException(status_code=400, detail=str(e)) from e
        # 立即生效：重新加载池并替换 chat_handler
        try:
            groups = repo.load_groups()
            handler: ChatHandler | None = getattr(request.app.state, "chat_handler", None)
            if handler is None:
                raise RuntimeError("chat_handler 未初始化")
            await handler.refresh_configuration(groups, config_repo=repo)
        except Exception as e:
            logger.exception("重载账号池失败")
            raise HTTPException(
                status_code=500, detail=f"配置已保存但重载失败: {e}"
            ) from e
        return {"status": "ok", "message": "配置已保存并生效"}

    @router.get("/config")
    def config_page() -> FileResponse:
        """配置页入口。"""
        path = STATIC_DIR / "config.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="配置页未就绪")
        return FileResponse(path)

    return router

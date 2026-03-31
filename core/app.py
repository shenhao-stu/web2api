"""
FastAPI 应用组装：配置加载、账号池、会话缓存、浏览器管理、插件注册、路由挂载。
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from core.account.pool import AccountPool
from core.api.auth import (
    AdminLoginAttemptStore,
    AdminSessionStore,
    configured_config_login_lock_seconds,
    configured_config_login_max_failures,
    config_login_enabled,
    ensure_config_secret_hashed,
    refresh_runtime_auth_settings,
)
from core.api.anthropic_routes import create_anthropic_router
from core.api.chat_handler import ChatHandler
from core.api.config_routes import create_config_router
from core.api.routes import create_router
from core.config.repository import create_config_repository
from core.config.settings import get, get_bool
from core.constants import CDP_PORT_RANGE, CHROMIUM_BIN
from core.plugin.base import PluginRegistry
from core.plugin.claude import register_claude_plugin
from core.runtime.browser_manager import BrowserManager
from core.runtime.session_cache import SessionCache

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"

_ACCOUNTS_ENV_KEY = "WEB2API_ACCOUNTS"


def _seed_accounts_from_env(repo) -> bool:
    """
    If WEB2API_ACCOUNTS is set and the database has no accounts yet,
    seed the database with accounts from the environment variable.

    Supported formats:
      - JSON array: full proxy group config (same as PUT /api/config body)
      - Simple comma-separated sessionKeys: creates one direct group with N claude accounts

    Returns True if accounts were seeded.
    """
    raw = os.environ.get(_ACCOUNTS_ENV_KEY, "").strip()
    if not raw:
        return False

    existing = repo.load_groups()
    if existing and any(g.accounts for g in existing):
        return False

    # Try JSON format first
    try:
        data = json.loads(raw)
        if isinstance(data, list) and data:
            from core.config.repository import _raw_to_groups
            groups = _raw_to_groups(data)
            repo.save_groups(groups)
            total = sum(len(g.accounts) for g in groups)
            logger.info(
                "[env-seed] loaded %d accounts from %s (JSON format)",
                total, _ACCOUNTS_ENV_KEY,
            )
            return True
    except (json.JSONDecodeError, TypeError):
        pass

    # Fall back to simple comma-separated sessionKeys
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        return False

    proxy_host = os.environ.get("WEB2API_ACCOUNTS_PROXY_HOST", "").strip()
    proxy_user = os.environ.get("WEB2API_ACCOUNTS_PROXY_USER", "").strip()
    proxy_pass = os.environ.get("WEB2API_ACCOUNTS_PROXY_PASS", "").strip()
    fingerprint_id = os.environ.get("WEB2API_ACCOUNTS_FINGERPRINT", "env-default").strip()
    use_proxy = bool(proxy_host)

    from core.config.schema import AccountConfig, ProxyGroupConfig
    accounts = [
        AccountConfig(
            name=f"claude-{i + 1}",
            type="claude",
            auth={"sessionKey": key},
        )
        for i, key in enumerate(keys)
    ]
    group = ProxyGroupConfig(
        proxy_host=proxy_host,
        proxy_user=proxy_user,
        proxy_pass=proxy_pass,
        fingerprint_id=fingerprint_id,
        use_proxy=use_proxy,
        timezone=os.environ.get("WEB2API_ACCOUNTS_TIMEZONE"),
        accounts=accounts,
    )
    repo.save_groups([group])
    logger.info(
        "[env-seed] loaded %d sessionKeys from %s (comma format, proxy=%s)",
        len(keys), _ACCOUNTS_ENV_KEY, use_proxy,
    )
    return True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时初始化配置与 ChatHandler，关闭时不做持久化（会话缓存进程内）。"""
    # 注册插件
    register_claude_plugin()

    repo = create_config_repository()
    repo.init_schema()
    ensure_config_secret_hashed(repo)
    _seed_accounts_from_env(repo)
    app.state.config_repo = repo
    auth_settings = refresh_runtime_auth_settings(app)
    groups = repo.load_groups()

    chromium_bin = (get("browser", "chromium_bin") or "").strip() or CHROMIUM_BIN
    headless = get_bool("browser", "headless", False)
    no_sandbox = get_bool("browser", "no_sandbox", False)
    disable_gpu = get_bool("browser", "disable_gpu", False)
    disable_gpu_sandbox = get_bool("browser", "disable_gpu_sandbox", False)
    cdp_wait_max_attempts = int(get("browser", "cdp_wait_max_attempts") or 90)
    cdp_wait_interval_seconds = float(
        get("browser", "cdp_wait_interval_seconds") or 2.0
    )
    cdp_wait_connect_timeout_seconds = float(
        get("browser", "cdp_wait_connect_timeout_seconds") or 2.0
    )
    port_start = int(get("browser", "cdp_port_start") or 9223)
    port_count = int(get("browser", "cdp_port_count") or 20)
    port_range = (
        list(range(port_start, port_start + port_count))
        if port_count > 0
        else list(CDP_PORT_RANGE)
    )
    api_keys = auth_settings.api_keys
    pool = AccountPool.from_groups(groups)
    session_cache = SessionCache()
    browser_manager = BrowserManager(
        chromium_bin=chromium_bin,
        headless=headless,
        no_sandbox=no_sandbox,
        disable_gpu=disable_gpu,
        disable_gpu_sandbox=disable_gpu_sandbox,
        port_range=port_range,
        cdp_wait_max_attempts=cdp_wait_max_attempts,
        cdp_wait_interval_seconds=cdp_wait_interval_seconds,
        cdp_wait_connect_timeout_seconds=cdp_wait_connect_timeout_seconds,
    )
    app.state.chat_handler = ChatHandler(
        pool=pool,
        session_cache=session_cache,
        browser_manager=browser_manager,
        config_repo=repo,
    )
    app.state.session_cache = session_cache
    app.state.browser_manager = browser_manager
    app.state.admin_sessions = AdminSessionStore()
    app.state.admin_login_attempts = AdminLoginAttemptStore(
        max_failures=configured_config_login_max_failures(),
        lock_seconds=configured_config_login_lock_seconds(),
    )
    if not groups:
        logger.warning("数据库无配置，服务已启动但当前无可用账号")
    if api_keys:
        logger.info("API 鉴权已启用，已加载 %d 个 API Key", len(api_keys))
    if auth_settings.config_login_enabled:
        logger.info(
            "配置页登录已启用，失败 %d 次锁定 %d 秒",
            app.state.admin_login_attempts.max_failures,
            app.state.admin_login_attempts.lock_seconds,
        )
    try:
        await app.state.chat_handler.prewarm_resident_browsers()
    except Exception:
        logger.exception("启动预热浏览器失败")
    app.state.maintenance_task = asyncio.create_task(
        app.state.chat_handler.run_maintenance_loop()
    )
    logger.info("服务已就绪，已注册 type: %s", ", ".join(PluginRegistry.all_types()))
    yield
    task = getattr(app.state, "maintenance_task", None)
    handler = getattr(app.state, "chat_handler", None)
    if handler is not None:
        await handler.shutdown()
    if task is not None:
        try:
            await task
        except asyncio.CancelledError:
            pass
    app.state.chat_handler = None


def create_app() -> FastAPI:
    app = FastAPI(
        title="Web2API(Plugin)",
        description="按 type 路由的 OpenAI 兼容接口，baseUrl: http://ip:port/{type}/v1/...",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", include_in_schema=False)
    def root() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz", include_in_schema=False)
    def healthz(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "config_login_enabled": config_login_enabled(request),
                "login": "/login",
                "config": "/config",
            }
        )

    app.include_router(create_router())
    app.include_router(create_anthropic_router())
    app.include_router(create_config_router())
    return app


app = create_app()

"""
Config routes: GET/PUT /api/config and the /config dashboard entrypoint.
"""

import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.api.auth import (
    ADMIN_SESSION_COOKIE,
    admin_logged_in,
    check_admin_login_rate_limit,
    configured_config_secret_hash,
    get_effective_auth_settings,
    hash_config_secret,
    normalize_api_key_text,
    record_admin_login_failure,
    record_admin_login_success,
    refresh_runtime_auth_settings,
    require_config_login,
    require_config_login_enabled,
    verify_config_secret,
)
from core.api.chat_handler import ChatHandler
from core.config.repository import (
    APP_SETTING_AUTH_API_KEY,
    APP_SETTING_AUTH_CONFIG_SECRET_HASH,
    APP_SETTING_ENABLE_PRO_MODELS,
    ConfigRepository,
)
from core.plugin.base import PluginRegistry

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class AdminLoginRequest(BaseModel):
    secret: str


class AuthSettingsUpdateRequest(BaseModel):
    api_key: str | None = None
    admin_password: str | None = None


class ProModelsUpdateRequest(BaseModel):
    enabled: bool = False


def _config_repo_of(request: Request) -> ConfigRepository:
    repo: ConfigRepository | None = getattr(request.app.state, "config_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Service is not ready")
    return repo


def _auth_settings_payload(request: Request) -> dict[str, Any]:
    settings = get_effective_auth_settings(request)
    return {
        "api_key": settings.api_key_text,
        "api_key_configured": bool(settings.api_keys),
        "api_key_source": settings.api_key_source,
        "api_key_env_managed": settings.api_key_env_managed,
        "admin_password_configured": bool(settings.config_secret_hash),
        "admin_password_source": settings.config_secret_source,
        "admin_password_env_managed": settings.config_secret_env_managed,
    }


def create_config_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/types")
    def get_types(_: None = Depends(require_config_login)) -> list[str]:
        """Return registered provider types for the config dashboard."""
        return PluginRegistry.all_types()

    @router.get("/api/config")
    def get_config(
        request: Request, _: None = Depends(require_config_login)
    ) -> list[dict[str, Any]]:
        """Return raw proxy-group and account configuration."""
        return _config_repo_of(request).load_raw()

    @router.get("/api/models/{provider}/metadata")
    def get_public_model_metadata(provider: str) -> dict[str, Any]:
        try:
            return PluginRegistry.model_metadata(provider)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/config/models")
    def get_model_metadata(_: None = Depends(require_config_login)) -> dict[str, Any]:
        return PluginRegistry.model_metadata("claude")

    @router.get("/api/config/auth-settings")
    def get_auth_settings(
        request: Request, _: None = Depends(require_config_login)
    ) -> dict[str, Any]:
        return _auth_settings_payload(request)

    @router.put("/api/config/auth-settings")
    def put_auth_settings(
        payload: AuthSettingsUpdateRequest,
        request: Request,
        _: None = Depends(require_config_login),
    ) -> dict[str, Any]:
        repo = _config_repo_of(request)
        if payload.api_key is not None:
            repo.set_app_setting(
                APP_SETTING_AUTH_API_KEY,
                normalize_api_key_text(payload.api_key),
            )
        if payload.admin_password is not None:
            password = payload.admin_password.strip()
            repo.set_app_setting(
                APP_SETTING_AUTH_CONFIG_SECRET_HASH,
                hash_config_secret(password) if password else "",
            )
        refresh_runtime_auth_settings(request.app)
        settings_payload = _auth_settings_payload(request)
        if payload.admin_password is not None and payload.admin_password.strip():
            store = getattr(request.app.state, "admin_sessions", None)
            if store is not None:
                token = (request.cookies.get(ADMIN_SESSION_COOKIE) or "").strip()
                store.revoke(token)
        return {"status": "ok", "settings": settings_payload}

    @router.get("/api/config/pro-models")
    def get_pro_models(
        request: Request, _: None = Depends(require_config_login)
    ) -> dict[str, Any]:
        repo = _config_repo_of(request)
        enabled = repo.get_app_setting(APP_SETTING_ENABLE_PRO_MODELS) == "true"
        return {"enabled": enabled}

    @router.put("/api/config/pro-models")
    def put_pro_models(
        payload: ProModelsUpdateRequest,
        request: Request,
        _: None = Depends(require_config_login),
    ) -> dict[str, Any]:
        repo = _config_repo_of(request)
        repo.set_app_setting(
            APP_SETTING_ENABLE_PRO_MODELS, "true" if payload.enabled else "false"
        )
        return {"status": "ok", "enabled": payload.enabled}

    @router.get("/api/config/status")
    def get_config_status(
        request: Request, _: None = Depends(require_config_login)
    ) -> dict[str, Any]:
        """Return runtime account status for the config dashboard."""
        repo = _config_repo_of(request)
        handler: ChatHandler | None = getattr(request.app.state, "chat_handler", None)
        if handler is None:
            raise HTTPException(status_code=503, detail="Service is not ready")
        runtime_status = handler.get_account_runtime_status()
        now = int(time.time())
        accounts: dict[str, dict[str, Any]] = {}
        for group in repo.load_groups():
            for account in group.accounts:
                account_id = f"{group.fingerprint_id}:{account.name}"
                runtime = runtime_status.get(account_id, {})
                is_frozen = (
                    account.unfreeze_at is not None and int(account.unfreeze_at) > now
                )
                accounts[account_id] = {
                    "fingerprint_id": group.fingerprint_id,
                    "account_name": account.name,
                    "enabled": account.enabled,
                    "unfreeze_at": account.unfreeze_at,
                    "is_frozen": is_frozen,
                    "is_active": bool(runtime.get("is_active")),
                    "tab_state": runtime.get("tab_state"),
                    "accepting_new": runtime.get("accepting_new"),
                    "active_requests": runtime.get("active_requests", 0),
                }
        return {"now": now, "accounts": accounts}

    @router.put("/api/config")
    async def put_config(
        request: Request,
        config: list[dict[str, Any]],
        _: None = Depends(require_config_login),
    ) -> dict[str, Any]:
        """Update configuration and apply it immediately."""
        repo = _config_repo_of(request)
        if not config:
            raise HTTPException(status_code=400, detail="Configuration must not be empty")
        for i, g in enumerate(config):
            if not isinstance(g, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"Item {i + 1} must be an object",
                )
            if "fingerprint_id" not in g:
                raise HTTPException(
                    status_code=400,
                    detail=f"Proxy group {i + 1} is missing field: fingerprint_id",
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
                    detail=f"Proxy group {i + 1} has proxy enabled and requires proxy_host",
                )
            accounts = g.get("accounts", [])
            if not accounts:
                raise HTTPException(
                    status_code=400,
                    detail=f"Proxy group {i + 1} must include at least one account",
                )
            for j, a in enumerate(accounts):
                if not isinstance(a, dict) or not (a.get("name") or "").strip():
                    raise HTTPException(
                        status_code=400,
                        detail=f"Account {j + 1} in proxy group {i + 1} must include name",
                    )
                if not (a.get("type") or "").strip():
                    raise HTTPException(
                        status_code=400,
                        detail=f"Account {j + 1} in proxy group {i + 1} must include type (for example: claude)",
                    )
                if "enabled" in a and not isinstance(
                    a.get("enabled"), (bool, int, str)
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Account {j + 1} in proxy group {i + 1} has an invalid enabled value",
                    )
        try:
            repo.save_raw(config)
        except Exception as e:
            logger.exception("Failed to save configuration")
            raise HTTPException(status_code=400, detail=str(e)) from e
        # Apply immediately: reload groups and refresh the active chat handler.
        try:
            groups = repo.load_groups()
            handler: ChatHandler | None = getattr(
                request.app.state, "chat_handler", None
            )
            if handler is None:
                raise RuntimeError("chat_handler is not initialized")
            await handler.refresh_configuration(groups, config_repo=repo)
        except Exception as e:
            logger.exception("Failed to reload account pool")
            raise HTTPException(
                status_code=500,
                detail=f"Configuration was saved but reload failed: {e}",
            ) from e
        return {"status": "ok", "message": "Configuration saved and applied"}

    @router.get("/login", response_model=None)
    def login_page(request: Request) -> FileResponse | RedirectResponse:
        require_config_login_enabled(request)
        if admin_logged_in(request):
            return RedirectResponse(url="/config", status_code=302)
        path = STATIC_DIR / "login.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Login page is not ready")
        return FileResponse(path)

    @router.post("/api/admin/login", response_model=None)
    def admin_login(payload: AdminLoginRequest, request: Request) -> Response:
        require_config_login_enabled(request)
        check_admin_login_rate_limit(request)
        secret = payload.secret.strip()
        encoded = configured_config_secret_hash(_config_repo_of(request))
        if not secret or not encoded or not verify_config_secret(secret, encoded):
            lock_seconds = record_admin_login_failure(request)
            if lock_seconds > 0:
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many failed login attempts. Try again in {lock_seconds} seconds.",
                )
            raise HTTPException(status_code=401, detail="Sign-in failed. Password is incorrect.")
        record_admin_login_success(request)
        store = request.app.state.admin_sessions
        token = store.create()
        response = JSONResponse({"status": "ok"})
        response.set_cookie(
            key=ADMIN_SESSION_COOKIE,
            value=token,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=store.ttl_seconds,
            path="/",
        )
        return response

    @router.post("/api/admin/logout", response_model=None)
    def admin_logout(request: Request) -> Response:
        token = (request.cookies.get(ADMIN_SESSION_COOKIE) or "").strip()
        store = getattr(request.app.state, "admin_sessions", None)
        if store is not None:
            store.revoke(token)
        response = JSONResponse({"status": "ok"})
        response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
        return response

    @router.get("/config", response_model=None)
    def config_page(request: Request) -> FileResponse | RedirectResponse:
        """配置页入口。"""
        require_config_login_enabled(request)
        if not admin_logged_in(request):
            return RedirectResponse(url="/login", status_code=302)
        path = STATIC_DIR / "config.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Config page is not ready")
        return FileResponse(path)

    return router

"""
Claude 插件：仅实现站点特有的上下文获取、会话创建、请求体构建、SSE 解析和限流处理。
其余编排逻辑（create_page / apply_auth / stream_completion 流程）全部由 BaseSitePlugin 完成。
调试时可在 config.yaml 的 claude.start_url、claude.api_base 指向 mock。
"""

import datetime
import json
import logging
import re
import time
from asyncio import Lock
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page

from core.api.schemas import InputAttachment
from core.constants import TIMEZONE
from core.plugin.base import BaseSitePlugin, PluginRegistry, SiteConfig
from core.plugin.errors import BrowserResourceInvalidError
from core.plugin.helpers import (
    _classify_browser_resource_error,
    clear_cookies_for_domain,
    clear_page_storage_for_switch,
    request_json_via_context_request,
    safe_page_reload,
    upload_file_via_context_request,
)

logger = logging.getLogger(__name__)

# Probe cache: skip redundant ensure_request_ready probes within this window.
_PROBE_CACHE_TTL_SECONDS = 60.0


def _truncate_url_for_log(value: str, limit: int = 200) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _safe_page_url(page: Page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 站点特有：请求体 & SSE 解析
# ---------------------------------------------------------------------------


def _default_completion_body(
    message: str,
    *,
    is_follow_up: bool = False,
    timezone: str = TIMEZONE,
    public_model: str = "",
) -> dict[str, Any]:
    """构建 Claude completion 请求体。续写时不带 create_conversation_params，否则 API 返回 400。"""
    body: dict[str, Any] = {
        "prompt": message,
        "timezone": timezone,
        "personalized_styles": [
            {
                "type": "default",
                "key": "Default",
                "name": "Normal",
                "nameKey": "normal_style_name",
                "prompt": "Normal\n",
                "summary": "Default responses from Claude",
                "summaryKey": "normal_style_summary",
                "isDefault": True,
            }
        ],
        "locale": "en-US",
        "tools": [
            {"type": "web_search_v0", "name": "web_search"},
            {"type": "artifacts_v0", "name": "artifacts"},
            {"type": "repl_v0", "name": "repl"},
            {"type": "widget", "name": "weather_fetch"},
            {"type": "widget", "name": "recipe_display_v0"},
            {"type": "widget", "name": "places_map_display_v0"},
            {"type": "widget", "name": "message_compose_v1"},
            {"type": "widget", "name": "ask_user_input_v0"},
            {"type": "widget", "name": "places_search"},
            {"type": "widget", "name": "fetch_sports_data"},
        ],
        "attachments": [],
        "files": [],
        "sync_sources": [],
        "rendering_mode": "messages",
    }
    if public_model == "claude-sonnet-4-6-thinking":
        body["model"] = "claude-sonnet-4-6"
    if not is_follow_up:
        body["create_conversation_params"] = {
            "name": "",
            "include_conversation_preferences": True,
            "is_temporary": False,
        }
        if public_model == "claude-sonnet-4-6-thinking":
            body["create_conversation_params"]["paprika_mode"] = "extended"
    return body


def _parse_one_sse_event(payload: str) -> tuple[list[str], str | None, str | None]:
    """解析单条 Claude SSE data 行，返回 (texts, message_id, error)。"""
    result: list[str] = []
    message_id: str | None = None
    error_message: str | None = None
    try:
        obj = json.loads(payload)
        if not isinstance(obj, dict):
            return (result, message_id, error_message)
        kind = obj.get("type")
        if kind == "error":
            err = obj.get("error") or {}
            error_message = err.get("message") or err.get("type") or "Unknown error"
            return (result, message_id, error_message)
        if "text" in obj and obj.get("text"):
            result.append(str(obj["text"]))
        elif kind == "content_block_delta":
            delta = obj.get("delta")
            if isinstance(delta, dict) and "text" in delta:
                result.append(str(delta["text"]))
            elif isinstance(delta, str) and delta:
                result.append(delta)
        elif kind == "message_start":
            msg = obj.get("message")
            if isinstance(msg, dict):
                for key in ("uuid", "id"):
                    if msg.get(key):
                        message_id = str(msg[key])
                        break
            if not message_id:
                mid = (
                    obj.get("message_uuid") or obj.get("uuid") or obj.get("message_id")
                )
                if mid:
                    message_id = str(mid)
        elif (
            kind
            and kind
            not in (
                "ping",
                "content_block_start",
                "content_block_stop",
                "message_stop",
                "message_delta",
                "message_limit",
            )
            and not result
        ):
            logger.debug(
                "SSE 未解析出正文 type=%s payload=%s",
                kind,
                payload[:200] if len(payload) > 200 else payload,
            )
    except json.JSONDecodeError:
        pass
    return (result, message_id, error_message)


def _is_terminal_sse_event(payload: str) -> bool:
    """Claude 正常流结束时会发送 message_stop。"""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and obj.get("type") == "message_stop"


# ---------------------------------------------------------------------------
# ClaudePlugin — 只需声明配置 + 实现 5 个 hook
# ---------------------------------------------------------------------------


class ClaudePlugin(BaseSitePlugin):
    """Claude Web2API plugin. auth must include sessionKey."""

    type_name = "claude"
    DEFAULT_MODEL_MAPPING = {
        "claude-sonnet-4-5": "claude-sonnet-4-5",
        "claude-sonnet-4.6": "claude-sonnet-4-6",
        "claude-sonnet-4-6-thinking": "claude-sonnet-4-6-thinking",
        "claude-haiku-4-5": "claude-haiku-4-5",
        "claude-opus-4-6": "claude-opus-4-6",
    }
    MODEL_ALIASES = {
        "s4": "claude-sonnet-4-6",
    }

    site = SiteConfig(
        start_url="https://claude.ai/login",
        api_base="https://claude.ai/api",
        cookie_name="sessionKey",
        cookie_domain=".claude.ai",
        auth_keys=["sessionKey", "session_key"],
        config_section="claude",
    )

    def __init__(self) -> None:
        super().__init__()
        # Per-page probe cache: page id -> last successful probe timestamp
        self._probe_ok_at: dict[int, float] = {}
        # Per-page navigation lock: prevents concurrent page.goto/reload
        self._nav_locks: dict[int, Lock] = {}
        # Per-page site_context cache: page id -> (context_dict, timestamp)
        self._site_context_cache: dict[int, tuple[dict[str, Any], float]] = {}

    _SITE_CONTEXT_TTL = 300.0  # 5 minutes

    def model_mapping(self) -> dict[str, str] | None:
        configured = super().model_mapping() or {}
        mapping = dict(self.DEFAULT_MODEL_MAPPING)
        mapping.update(configured)
        for alias, upstream_model in self.MODEL_ALIASES.items():
            mapping.setdefault(alias, upstream_model)
        return mapping

    def listed_model_mapping(self) -> dict[str, str]:
        configured = super().model_mapping() or {}
        mapping = dict(self.DEFAULT_MODEL_MAPPING)
        mapping.update(configured)
        for alias in self.MODEL_ALIASES:
            mapping.pop(alias, None)
        return mapping

    async def apply_auth(
        self,
        context: BrowserContext,
        page: Page,
        auth: dict[str, Any],
        *,
        reload: bool = True,
    ) -> None:
        await clear_cookies_for_domain(context, self.site.cookie_domain)
        await clear_page_storage_for_switch(page)
        await super().apply_auth(context, page, auth, reload=False)
        if reload:
            await safe_page_reload(page, url=self.start_url)

    def _is_claude_domain(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().lstrip(".")
        if not host:
            return False
        allowed_hosts = {"claude.ai", "claude.com"}
        for configured_url in (self.start_url, self.api_base):
            configured_host = (urlparse(configured_url).hostname or "").lower().lstrip(".")
            if configured_host:
                allowed_hosts.add(configured_host)
        return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)

    def _suspicious_page_reason(self, url: str) -> str | None:
        if not url:
            return "empty_page_url"
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return "invalid_page_url"
        if not self._is_claude_domain(url):
            return "non_claude_domain"
        path = parsed.path or "/"
        if path == "/new" or path.startswith("/new/"):
            return "new_chat_page"
        if path in {"/logout", "/auth", "/signed-out"}:
            return "logout_page"
        if path.startswith("/signup"):
            return "signup_page"
        if path == "/app-unavailable-in-region" or path.startswith(
            "/app-unavailable-in-region/"
        ):
            return "app_unavailable_in_region"
        return None

    def _is_suspicious_page_url(self, url: str) -> bool:
        return self._suspicious_page_reason(url) is not None

    async def _probe_request_ready(
        self,
        context: BrowserContext,
        page: Page,
        *,
        request_id: str,
    ) -> tuple[bool, str | None]:
        current_url = _safe_page_url(page)
        suspicious_reason = self._suspicious_page_reason(current_url)
        if suspicious_reason is not None:
            logger.warning(
                "[%s] request-ready probe sees suspicious page url request_id=%s reason=%s page.url=%s",
                self.type_name,
                request_id,
                suspicious_reason,
                _truncate_url_for_log(current_url),
            )
            return (False, suspicious_reason)
        try:
            site_context = await self.fetch_site_context(
                context,
                page,
                request_id=request_id,
            )
        except BrowserResourceInvalidError:
            raise
        except Exception as e:
            logger.warning(
                "[%s] request-ready probe failed request_id=%s page.url=%s err=%s",
                self.type_name,
                request_id,
                _truncate_url_for_log(current_url),
                str(e)[:240],
            )
            return (False, f"control_probe_error:{str(e)[:120]}")
        return (site_context is not None, None if site_context is not None else "account_probe_empty")

    async def ensure_request_ready(
        self,
        context: BrowserContext,
        page: Page,
        *,
        request_id: str = "",
        session_id: str | None = None,
        phase: str = "",
        account_id: str = "",
    ) -> None:
        initial_url = _safe_page_url(page)
        current_url = initial_url
        probe_request_id = request_id or f"ready:{phase or 'request'}"
        action = "none"
        probe_before = False
        probe_after = False
        probe_reason: str | None = None
        page_id = id(page)

        # Fast path (lock-free): page URL is clean and probe succeeded recently.
        suspicious_reason = self._suspicious_page_reason(current_url)
        if suspicious_reason is None:
            last_ok = self._probe_ok_at.get(page_id, 0.0)
            if (time.time() - last_ok) < _PROBE_CACHE_TTL_SECONDS:
                return
        if suspicious_reason == "app_unavailable_in_region":
            raise RuntimeError(
                "Claude page is app-unavailable-in-region; the runtime IP or region cannot reach Claude Web"
            )

        # Slow path: acquire per-page nav lock to prevent concurrent navigation.
        nav_lock = self._nav_locks.setdefault(page_id, Lock())
        async with nav_lock:
            # Re-check after acquiring lock — another request may have fixed the page.
            current_url = _safe_page_url(page)
            suspicious_reason = self._suspicious_page_reason(current_url)
            if suspicious_reason is None:
                last_ok = self._probe_ok_at.get(page_id, 0.0)
                if (time.time() - last_ok) < _PROBE_CACHE_TTL_SECONDS:
                    return
            if suspicious_reason == "app_unavailable_in_region":
                raise RuntimeError(
                    "Claude page is app-unavailable-in-region; the runtime IP or region cannot reach Claude Web"
                )

            try:
                if suspicious_reason is not None:
                    action = "goto"
                    try:
                        await safe_page_reload(page, url=self.start_url)
                    except Exception as e:
                        classified = _classify_browser_resource_error(
                            e,
                            helper_name="claude.ensure_request_ready",
                            operation="preflight",
                            stage="goto_start_url",
                            request_url=self.start_url,
                            page=page,
                            request_id=request_id or None,
                            stream_phase=phase or None,
                        )
                        if classified is not None:
                            raise classified from e
                        raise
                    current_url = _safe_page_url(page)
                    suspicious_reason = self._suspicious_page_reason(current_url)
                    if suspicious_reason == "app_unavailable_in_region":
                        probe_reason = suspicious_reason
                        raise RuntimeError(
                            "Claude page is app-unavailable-in-region after goto; the runtime IP or region cannot reach Claude Web"
                        )

                probe_before = self._suspicious_page_reason(current_url) is None
                if probe_before:
                    probe_after, probe_reason = await self._probe_request_ready(
                        context,
                        page,
                        request_id=f"{probe_request_id}:initial",
                    )
                    if probe_after:
                        self._probe_ok_at[page_id] = time.time()
                        return
                    if probe_reason == "app_unavailable_in_region":
                        raise RuntimeError(
                            "Claude page is app-unavailable-in-region during control probe; the runtime IP or region cannot reach Claude Web"
                        )
                else:
                    probe_after = False
                    probe_reason = suspicious_reason or "suspicious_page_url"

                action = "reload"
                try:
                    await safe_page_reload(page)
                except Exception as e:
                    classified = _classify_browser_resource_error(
                        e,
                        helper_name="claude.ensure_request_ready",
                        operation="preflight",
                        stage="reload",
                        request_url=current_url or self.start_url,
                        page=page,
                        request_id=request_id or None,
                        stream_phase=phase or None,
                    )
                    if classified is not None:
                        raise classified from e
                    raise
                current_url = _safe_page_url(page)
                if self._suspicious_page_reason(current_url) == "app_unavailable_in_region":
                    probe_reason = "app_unavailable_in_region"
                    raise RuntimeError(
                        "Claude page is app-unavailable-in-region after reload; the runtime IP or region cannot reach Claude Web"
                    )
                probe_after, probe_reason = await self._probe_request_ready(
                    context,
                    page,
                    request_id=f"{probe_request_id}:reload",
                )
                if probe_after:
                    self._probe_ok_at[page_id] = time.time()
                    return
                if probe_reason == "app_unavailable_in_region":
                    raise RuntimeError(
                        "Claude page is app-unavailable-in-region after reload probe; the runtime IP or region cannot reach Claude Web"
                    )

                action = "goto"
                try:
                    await safe_page_reload(page, url=self.start_url)
                except Exception as e:
                    classified = _classify_browser_resource_error(
                        e,
                        helper_name="claude.ensure_request_ready",
                        operation="preflight",
                        stage="goto_start_url",
                        request_url=self.start_url,
                        page=page,
                        request_id=request_id or None,
                        stream_phase=phase or None,
                    )
                    if classified is not None:
                        raise classified from e
                    raise
                current_url = _safe_page_url(page)
                if self._suspicious_page_reason(current_url) == "app_unavailable_in_region":
                    probe_reason = "app_unavailable_in_region"
                    raise RuntimeError(
                        "Claude page is app-unavailable-in-region after page correction; the runtime IP or region cannot reach Claude Web"
                    )
                probe_after, probe_reason = await self._probe_request_ready(
                    context,
                    page,
                    request_id=f"{probe_request_id}:goto",
                )
                if not probe_after:
                    if probe_reason == "suspicious_page_url":
                        raise BrowserResourceInvalidError(
                            "Claude request preflight failed after page correction: suspicious_page_url",
                            helper_name="claude.ensure_request_ready",
                            operation="preflight",
                            stage="probe_after_goto",
                            resource_hint="page",
                            request_url=self.start_url,
                            page_url=current_url,
                            request_id=request_id or None,
                            stream_phase=phase or None,
                        )
                    raise RuntimeError(
                        f"Claude request control probe failed after page correction: {probe_reason or 'unknown'}"
                    )
                self._probe_ok_at[page_id] = time.time()
            finally:
                logger.info(
                    "[%s] ensure_request_ready phase=%s account=%s session_id=%s action=%s probe_before=%s probe_after=%s probe_reason=%s page.url.before=%s page.url.after=%s",
                    self.type_name,
                    phase,
                    account_id,
                    session_id,
                    action,
                    probe_before,
                    probe_after,
                    probe_reason,
                    _truncate_url_for_log(initial_url),
                    _truncate_url_for_log(current_url),
            )

    # ---- 5 个必须实现的 hook ----

    async def fetch_site_context(
        self,
        context: BrowserContext,
        page: Page,
        request_id: str = "",
    ) -> dict[str, Any] | None:
        page_id = id(page)
        cached = self._site_context_cache.get(page_id)
        if cached is not None:
            ctx, ts = cached
            if (time.time() - ts) < self._SITE_CONTEXT_TTL:
                return ctx
        resp = await request_json_via_context_request(
            context,
            page,
            f"{self.api_base}/account",
            timeout_ms=15000,
            request_id=request_id or "site-context",
        )
        if int(resp.get("status") or 0) != 200:
            text = str(resp.get("text") or "")[:500]
            logger.warning(
                "[%s] fetch_site_context 失败 status=%s url=%s body=%s",
                self.type_name,
                resp.get("status"),
                resp.get("url"),
                text,
            )
            return None
        data = resp.get("json")
        if not isinstance(data, dict):
            logger.warning("[%s] fetch_site_context 返回非 JSON", self.type_name)
            return None
        memberships = data.get("memberships") or []
        if not memberships:
            return None
        org = memberships[0].get("organization") or {}
        org_uuid = org.get("uuid")
        if org_uuid:
            result = {"org_uuid": org_uuid}
            self._site_context_cache[page_id] = (result, time.time())
            return result
        return None

    async def create_session(
        self,
        context: BrowserContext,
        page: Page,
        site_context: dict[str, Any],
        **kwargs: Any,
    ) -> str | None:
        org_uuid = site_context["org_uuid"]
        public_model = str(kwargs.get("public_model") or "").strip()
        upstream_model = str(kwargs.get("upstream_model") or "").strip()
        if not upstream_model:
            upstream_model = self.resolve_model(None).upstream_model
        payload: dict[str, Any] = {
            "name": "",
            "model": (
                "claude-sonnet-4-6"
                if public_model == "claude-sonnet-4-6-thinking"
                else upstream_model
            ),
        }
        if public_model == "claude-sonnet-4-6-thinking":
            payload["paprika_mode"] = "extended"
        url = f"{self.api_base}/organizations/{org_uuid}/chat_conversations"
        request_id = str(kwargs.get("request_id") or "").strip()
        resp = await request_json_via_context_request(
            context,
            page,
            url,
            method="POST",
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout_ms=15000,
            request_id=request_id or f"create-session:{org_uuid}",
        )
        status = int(resp.get("status") or 0)
        if status not in (200, 201):
            text = str(resp.get("text") or "")[:500]
            logger.warning("创建会话失败 %s: %s", status, text)
            return None
        data = resp.get("json")
        if not isinstance(data, dict):
            logger.warning("创建会话返回非 JSON")
            return None
        return data.get("uuid")

    def build_completion_url(self, session_id: str, state: dict[str, Any]) -> str:
        org_uuid = state["site_context"]["org_uuid"]
        return f"{self.api_base}/organizations/{org_uuid}/chat_conversations/{session_id}/completion"

    # 构建请求体
    def build_completion_body(
        self,
        message: str,
        session_id: str,
        state: dict[str, Any],
        prepared_attachments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parent = state.get("parent_message_uuid")
        tz = state.get("timezone") or TIMEZONE
        public_model = str(state.get("public_model") or "").strip()
        body = _default_completion_body(
            message,
            is_follow_up=parent is not None,
            timezone=tz,
            public_model=public_model,
        )
        if parent:
            body["parent_message_uuid"] = parent
        if prepared_attachments:
            body.update(prepared_attachments)
        return body

    def parse_stream_event(
        self,
        payload: str,
    ) -> tuple[list[str], str | None, str | None]:
        return _parse_one_sse_event(payload)

    def is_stream_end_event(self, payload: str) -> bool:
        return _is_terminal_sse_event(payload)

    # 处理错误
    def stream_transport(self) -> str:
        return "context_request"

    def on_http_error(
        self,
        message: str,
        headers: dict[str, str] | None,
    ) -> int | None:
        if "429" not in message:
            return None
        if headers:
            reset = headers.get("anthropic-ratelimit-requests-reset") or headers.get(
                "Anthropic-Ratelimit-Requests-Reset"
            )
            if reset:
                try:
                    s = str(reset).strip()
                    if s.endswith("Z"):
                        s = s[:-1] + "+00:00"
                    dt = datetime.datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    return int(dt.timestamp())
                except Exception:
                    pass
        return int(time.time()) + 5 * 3600

    _UUID_RE = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )

    def on_stream_completion_finished(
        self,
        session_id: str,
        message_ids: list[str],
    ) -> None:
        """Claude 多轮续写需要 parent_message_uuid，取本轮最后一条消息 UUID 写入 state。"""
        last_uuid = next(
            (m for m in reversed(message_ids) if self._UUID_RE.match(m)), None
        )
        if last_uuid and session_id in self._session_state:
            self._session_state[session_id]["parent_message_uuid"] = last_uuid
            logger.info(
                "[%s] updated parent_message_uuid=%s", self.type_name, last_uuid
            )

    async def prepare_attachments(
        self,
        context: BrowserContext,
        page: Page,
        session_id: str,
        state: dict[str, Any],
        attachments: list[InputAttachment],
        request_id: str = "",
    ) -> dict[str, Any]:
        if not attachments:
            return {}
        if len(attachments) > 5:
            raise RuntimeError("Claude 单次最多上传 5 张图片")

        org_uuid = state["site_context"]["org_uuid"]
        url = (
            f"{self.api_base}/organizations/{org_uuid}/conversations/"
            f"{session_id}/wiggle/upload-file"
        )
        file_ids: list[str] = []
        for attachment in attachments:
            resp = await upload_file_via_context_request(
                context,
                page,
                url,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                data=attachment.data,
                field_name="file",
                timeout_ms=30000,
                request_id=request_id or f"upload:{session_id}",
            )
            status = int(resp.get("status") or 0)
            if status not in (200, 201):
                text = str(resp.get("text") or "")[:500]
                raise RuntimeError(f"图片上传失败 {status}: {text}")
            data = resp.get("json")
            if not isinstance(data, dict):
                raise RuntimeError("图片上传返回非 JSON")
            file_uuid = data.get("file_uuid") or data.get("uuid")
            if not file_uuid:
                raise RuntimeError("图片上传未返回 file_uuid")
            file_ids.append(str(file_uuid))
        return {"attachments": [], "files": file_ids}


def register_claude_plugin() -> None:
    """注册 Claude 插件到全局 Registry。"""
    PluginRegistry.register(ClaudePlugin())

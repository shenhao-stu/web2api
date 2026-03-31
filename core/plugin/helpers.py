"""
插件通用能力：页面复用、Cookie 登录、在浏览器内发起 fetch 并流式回传。
接入方只需实现站点特有的 URL/请求体/SSE 解析，其余复用此处逻辑。
"""

import asyncio
import base64
import codecs
import json
import logging
from collections.abc import Callable
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from curl_cffi import requests as curl_requests
from playwright.async_api import BrowserContext, Page

from core.plugin.errors import AccountFrozenError, BrowserResourceInvalidError

ParseSseEvent = Callable[[str], tuple[list[str], str | None, str | None]]

logger = logging.getLogger(__name__)

_BROWSER_RESOURCE_ERROR_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("target crashed", "page", "target_crashed"),
    ("page crashed", "browser", "page_crashed"),
    ("execution context was destroyed", "page", "execution_context_destroyed"),
    ("navigating frame was detached", "page", "frame_detached"),
    ("frame was detached", "page", "frame_detached"),
    ("session closed. most likely the page has been closed", "page", "page_closed"),
    ("most likely the page has been closed", "page", "page_closed"),
    ("browser context has been closed", "page", "context_closed"),
    ("context has been closed", "page", "context_closed"),
    ("target page, context or browser has been closed", "page", "page_or_browser_closed"),
    ("page has been closed", "page", "page_closed"),
    ("target closed", "page", "target_closed"),
    ("browser has been closed", "browser", "browser_closed"),
    ("browser closed", "browser", "browser_closed"),
    ("connection closed", "browser", "browser_disconnected"),
    ("connection terminated", "browser", "browser_disconnected"),
    ("has been disconnected", "browser", "browser_disconnected"),
    # Proxy / network tunnel errors — retryable via browser re-launch
    ("err_tunnel_connection_failed", "browser", "proxy_tunnel_failed"),
    ("err_proxy_connection_failed", "browser", "proxy_connection_failed"),
    ("err_connection_refused", "browser", "connection_refused"),
    ("err_connection_timed_out", "browser", "connection_timed_out"),
    ("err_connection_reset", "browser", "connection_reset"),
)


def _truncate_for_log(value: str, limit: int = 240) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "..."



def _safe_page_url(page: Page | None) -> str:
    if page is None:
        return ""
    try:
        return page.url or ""
    except Exception:
        return ""



def _evaluate_timeout_seconds(timeout_ms: int, grace_seconds: float = 5.0) -> float:
    return max(5.0, float(timeout_ms) / 1000.0 + grace_seconds)



def _consume_background_task_result(task: asyncio.Task[Any]) -> None:
    try:
        if not task.cancelled():
            task.exception()
    except Exception:
        pass



def _classify_browser_resource_error(
    exc: Exception,
    *,
    helper_name: str,
    operation: str,
    stage: str,
    request_url: str,
    page: Page | None,
    request_id: str | None = None,
    stream_phase: str | None = None,
) -> BrowserResourceInvalidError | None:
    message = str(exc).strip() or exc.__class__.__name__
    normalized = message.lower()
    for pattern, resource_hint, reason in _BROWSER_RESOURCE_ERROR_PATTERNS:
        if pattern not in normalized:
            continue
        page_url = _safe_page_url(page)
        logger.warning(
            "[browser-resource-invalid] helper=%s operation=%s stage=%s reason=%s resource=%s request_id=%s stream_phase=%s request_url=%s page.url=%s err=%s",
            helper_name,
            operation,
            stage,
            reason,
            resource_hint,
            request_id,
            stream_phase,
            _truncate_for_log(request_url),
            _truncate_for_log(page_url),
            _truncate_for_log(message, 400),
        )
        return BrowserResourceInvalidError(
            message,
            helper_name=helper_name,
            operation=operation,
            stage=stage,
            resource_hint=resource_hint,
            request_url=request_url,
            page_url=page_url,
            request_id=request_id,
            stream_phase=stream_phase,
        )
    return None

# 在页面内 POST 请求并流式回传：成功时逐块发送响应体，失败时发送 __error__: 前缀 + 信息，最后发送 __done__
# bindingName 按请求唯一，同一 page 多并发时互不串数据
PAGE_FETCH_STREAM_JS = """
async ({ url, body, bindingName, timeoutMs }) => {
  const send = globalThis[bindingName];
  const done = "__done__";
  const errPrefix = "__error__:";
  try {
    const ctrl = new AbortController();
    const effectiveTimeoutMs = timeoutMs || 90000;
    const t = setTimeout(() => ctrl.abort(), effectiveTimeoutMs);
    const resp = await fetch(url, {
      method: "POST",
      body: body,
      headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
      credentials: "include",
      signal: ctrl.signal
    });
    clearTimeout(t);
    if (!resp.ok) {
      const errText = await resp.text();
      const errSnippet = (errText && errText.length > 800) ? errText.slice(0, 800) + "..." : (errText || "");
      await send(errPrefix + "HTTP " + resp.status + " " + errSnippet);
      await send(done);
      return;
    }
    if (!resp.body) {
      await send(errPrefix + "No response body");
      await send(done);
      return;
    }
    const headersObj = {};
    resp.headers.forEach((v, k) => { headersObj[k] = v; });
    await send("__headers__:" + JSON.stringify(headersObj));
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    while (true) {
      const { done: streamDone, value } = await reader.read();
      if (streamDone) break;
      await send(dec.decode(value));
    }
  } catch (e) {
    const msg = e.name === "AbortError" ? `请求超时(${Math.floor(effectiveTimeoutMs / 1000)}s)` : (e.message || String(e));
    await send(errPrefix + msg);
  }
  await send(done);
}
"""


PAGE_FETCH_JSON_JS = """
async ({ url, method, body, headers, timeoutMs }) => {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs || 15000);
  try {
    const resp = await fetch(url, {
      method: method || "GET",
      body: body ?? undefined,
      headers: headers || {},
      credentials: "include",
      signal: ctrl.signal
    });
    clearTimeout(t);
    const text = await resp.text();
    const headersObj = {};
    resp.headers.forEach((v, k) => { headersObj[k] = v; });
    return {
      ok: resp.ok,
      status: resp.status,
      statusText: resp.statusText,
      url: resp.url,
      redirected: resp.redirected,
      headers: headersObj,
      text,
    };
  } catch (e) {
    clearTimeout(t);
    const msg = e.name === "AbortError" ? `请求超时(${Math.floor((timeoutMs || 15000) / 1000)}s)` : (e.message || String(e));
    return { error: msg };
  }
}
"""


PAGE_FETCH_MULTIPART_JS = """
async ({ url, filename, mimeType, dataBase64, fieldName, extraFields, timeoutMs }) => {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs || 30000);
  try {
    const binary = atob(dataBase64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    const form = new FormData();
    if (extraFields) {
      Object.entries(extraFields).forEach(([k, v]) => {
        if (v !== undefined && v !== null) form.append(k, String(v));
      });
    }
    const file = new File([bytes], filename, { type: mimeType || "application/octet-stream" });
    form.append(fieldName || "file", file);
    const resp = await fetch(url, {
      method: "POST",
      body: form,
      credentials: "include",
      signal: ctrl.signal
    });
    clearTimeout(t);
    const text = await resp.text();
    const headersObj = {};
    resp.headers.forEach((v, k) => { headersObj[k] = v; });
    return {
      ok: resp.ok,
      status: resp.status,
      statusText: resp.statusText,
      url: resp.url,
      redirected: resp.redirected,
      headers: headersObj,
      text,
    };
  } catch (e) {
    clearTimeout(t);
    const msg = e.name === "AbortError" ? `请求超时(${Math.floor((timeoutMs || 30000) / 1000)}s)` : (e.message || String(e));
    return { error: msg };
  }
}
"""


async def ensure_page_for_site(
    context: BrowserContext,
    url_contains: str,
    start_url: str,
    *,
    timeout: int = 45000,
) -> Page:
    """
    若已有页面 URL 包含 url_contains 则复用，否则 new_page 并 goto start_url。
    接入方只需提供「站点特征」和「入口 URL」。
    """
    if context.pages:
        for p in context.pages:
            if url_contains in (p.url or ""):
                return p
    page = await context.new_page()
    await page.goto(start_url, wait_until="commit", timeout=timeout)
    return page


async def create_page_for_site(
    context: BrowserContext,
    start_url: str,
    *,
    reuse_page: Page | None = None,
    timeout: int = 45000,
) -> Page:
    """
    若传入 reuse_page 则在其上 goto start_url，否则 new_page 再 goto。
    用于复用浏览器默认空白页或 page 池的初始化与补回。
    """
    if reuse_page is not None:
        await reuse_page.goto(start_url, wait_until="commit", timeout=timeout)
        return reuse_page
    page = await context.new_page()
    await page.goto(start_url, wait_until="commit", timeout=timeout)
    return page


def _cookie_domain_matches(cookie_domain: str, site_domain: str) -> bool:
    """判断 cookie 的 domain 是否属于站点 domain（如 .claude.ai 与 claude.ai 视为同一域）。"""
    a = cookie_domain if cookie_domain.startswith(".") else f".{cookie_domain}"
    b = site_domain if site_domain.startswith(".") else f".{site_domain}"
    return a == b


def _cookie_to_set_param(c: Any) -> dict[str, str]:
    """将 context.cookies() 返回的项转为 add_cookies 接受的 SetCookieParam 格式。"""
    return {
        "name": c["name"],
        "value": c["value"],
        "domain": c.get("domain") or "",
        "path": c.get("path") or "/",
    }


async def clear_cookies_for_domain(
    context: BrowserContext,
    site_domain: str,
) -> None:
    """清除 context 内属于指定站点域的所有 cookie，保留其他域。"""
    cookies = await context.cookies()
    keep = [
        c
        for c in cookies
        if not _cookie_domain_matches(c.get("domain", ""), site_domain)
    ]
    await context.clear_cookies()
    if keep:
        await context.add_cookies([_cookie_to_set_param(c) for c in keep])  # type: ignore[arg-type]
    logger.info(
        "[auth] cleared cookies for domain=%s (kept %s cookies)", site_domain, len(keep)
    )


async def clear_page_storage_for_switch(page: Page) -> None:
    """切号前清空当前页面的 localStorage（当前 origin）。"""
    try:
        await page.evaluate("() => { window.localStorage.clear(); }")
        logger.info("[auth] cleared localStorage for switch")
    except Exception as e:
        logger.warning("[auth] clear localStorage failed (page may be detached): %s", e)


async def safe_page_reload(page: Page, url: str | None = None) -> None:
    """安全地 reload 或 goto(url)，忽略因 ERR_ABORTED / frame detached 导致的异常。"""
    try:
        if url:
            await page.goto(url, wait_until="commit", timeout=45000)
        else:
            await page.reload(wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        err_msg = str(e)
        if "ERR_ABORTED" in err_msg or "detached" in err_msg.lower():
            logger.warning(
                "[auth] page.reload/goto 被中止或 frame 已分离: %s", err_msg[:200]
            )
        else:
            raise


async def apply_cookie_auth(
    context: BrowserContext,
    page: Page,
    auth: dict[str, Any],
    cookie_name: str,
    auth_keys: list[str],
    domain: str,
    *,
    path: str = "/",
    reload: bool = True,
) -> None:
    """
    从 auth 中按 auth_keys 顺序取第一个非空值作为 cookie 值，写入 context 并可选 reload。
    接入方只需提供 cookie 名、auth 里的 key 列表、域名。
    仅写 cookie 不 reload 时，同 context 内的 fetch() 仍会带上 cookie；reload 仅在需要页面文档同步登录态时用。
    """
    value = None
    for k in auth_keys:
        v = auth.get(k)
        if v is not None and v != "":
            value = str(v).strip()
            if value:
                break
    if not value:
        raise ValueError(f"auth 需包含以下其一且非空: {auth_keys}")

    logger.info(
        "[auth] context.add_cookies domain=%s name=%s reload=%s page.url=%s",
        domain,
        cookie_name,
        reload,
        page.url,
    )
    await context.add_cookies(
        [
            {
                "name": cookie_name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": True,
                "httpOnly": True,
            }
        ]
    )
    if reload:
        await safe_page_reload(page)


def _attach_json_body(result: dict[str, Any], *, invalid_message: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError(invalid_message)
    error = result.get("error")
    if error:
        raise RuntimeError(str(error))
    text = result.get("text")
    if isinstance(text, str) and text:
        try:
            result["json"] = json.loads(text)
        except json.JSONDecodeError:
            result["json"] = None
    else:
        result["json"] = None
    return result


def _cookie_domain_matches_url(cookie_domain: str, target_url: str) -> bool:
    host = (urlparse(target_url).hostname or "").lower().lstrip(".")
    domain = (cookie_domain or "").lower().lstrip(".")
    if not host or not domain:
        return False
    return host == domain or host.endswith(f".{domain}")


def _cookies_for_url(cookies: list[dict[str, Any]], target_url: str) -> dict[str, str]:
    target_host = (urlparse(target_url).hostname or "").lower().lstrip(".")
    if not target_host:
        return {}
    selected: dict[str, str] = {}
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "").strip()
        if not name or not _cookie_domain_matches_url(domain, target_url):
            continue
        selected[name] = value
    return selected


async def _stream_via_http_client(
    context: BrowserContext,
    page: Page | None,
    url: str,
    body: str,
    request_id: str,
    *,
    on_http_error: Callable[[str, dict[str, str] | None], int | None] | None = None,
    on_headers: Callable[[dict[str, str]], None] | None = None,
    connect_timeout: float = 30.0,
    read_timeout: float = 300.0,
    impersonate: str = "chrome142",
    proxy_url: str | None = None,
    proxy_auth: tuple[str, str] | None = None,
) -> AsyncIterator[str]:
    logger.info(
        "[fetch] helper=stream_raw_via_context_request request_id=%s stage=http_client url=%s page.url=%s",
        request_id,
        _truncate_for_log(url, 120),
        _truncate_for_log(_safe_page_url(page), 120),
    )

    parsed = urlparse(url)
    referer = ""
    if parsed.scheme and parsed.netloc:
        referer = f"{parsed.scheme}://{parsed.netloc}/"

    try:
        cookies = await context.cookies([url])
    except Exception as e:
        classified = _classify_browser_resource_error(
            e,
            helper_name="stream_raw_via_context_request",
            operation="context.cookies",
            stage="load_cookies",
            request_url=url,
            page=page,
            request_id=request_id,
            stream_phase="fetch",
        )
        if classified is not None:
            raise classified from e
        raise BrowserResourceInvalidError(
            str(e),
            helper_name="stream_raw_via_context_request",
            operation="context.cookies",
            stage="load_cookies",
            resource_hint="page",
            request_url=url,
            page_url=_safe_page_url(page),
            request_id=request_id,
            stream_phase="fetch",
        ) from e
    cookie_jar = _cookies_for_url(cookies, url)
    session_kwargs: dict[str, Any] = {
        "impersonate": impersonate,
        "timeout": (connect_timeout, read_timeout),
        "verify": True,
        "allow_redirects": True,
        "default_headers": True,
    }
    if cookie_jar:
        session_kwargs["cookies"] = cookie_jar
    if proxy_url:
        session_kwargs["proxy"] = proxy_url
    if proxy_auth:
        session_kwargs["proxy_auth"] = proxy_auth

    response = None
    try:
        async with curl_requests.AsyncSession(**session_kwargs) as session:
            try:
                request_headers = {
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                }
                if referer:
                    request_headers["Origin"] = referer.rstrip("/")
                async with session.stream(
                    "POST",
                    url,
                    data=body.encode("utf-8"),
                    headers=request_headers,
                ) as response:
                    headers = {
                        str(k).lower(): str(v) for k, v in response.headers.items()
                    }
                    if on_headers:
                        on_headers(headers)

                    status = int(response.status_code)
                    if status < 200 or status >= 300:
                        body_parts: list[str] = []
                        decoder = codecs.getincrementaldecoder("utf-8")("replace")
                        async for chunk in response.aiter_content():
                            if not chunk:
                                continue
                            body_parts.append(decoder.decode(chunk))
                            if sum(len(part) for part in body_parts) >= 800:
                                break
                        body_parts.append(decoder.decode(b"", final=True))
                        snippet = "".join(body_parts)
                        if len(snippet) > 800:
                            snippet = snippet[:800] + "..."
                        msg = f"HTTP {status} {snippet}".strip()
                        if on_http_error:
                            unfreeze_at = on_http_error(msg, headers)
                            if isinstance(unfreeze_at, int):
                                logger.warning("[fetch] HTTP error from context request: %s", msg)
                                raise AccountFrozenError(msg, unfreeze_at)
                        raise RuntimeError(msg)

                    decoder = codecs.getincrementaldecoder("utf-8")("replace")
                    async for chunk in response.aiter_content():
                        if not chunk:
                            continue
                        text = decoder.decode(chunk)
                        if text:
                            yield text
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        yield tail
            except Exception as e:
                classified = _classify_browser_resource_error(
                    e,
                    helper_name="stream_raw_via_context_request",
                    operation="http_client",
                    stage="stream",
                    request_url=url,
                    page=page,
                    request_id=request_id,
                    stream_phase="body",
                )
                if classified is not None:
                    raise classified from e
                raise BrowserResourceInvalidError(
                    str(e),
                    helper_name="stream_raw_via_context_request",
                    operation="http_client",
                    stage="stream",
                    resource_hint="transport",
                    request_url=url,
                    page_url=_safe_page_url(page),
                    request_id=request_id,
                    stream_phase="body",
                ) from e
    except AccountFrozenError:
        raise
    except BrowserResourceInvalidError:
        raise
    except Exception as e:
        classified = _classify_browser_resource_error(
            e,
            helper_name="stream_raw_via_context_request",
            operation="http_client",
            stage="request",
            request_url=url,
            page=page,
            request_id=request_id,
            stream_phase="fetch",
        )
        if classified is not None:
            raise classified from e
        logger.warning(
            "[fetch] helper=stream_raw_via_context_request request_id=%s http_client failed url=%s page.url=%s err=%s",
            request_id,
            _truncate_for_log(url, 120),
            _truncate_for_log(_safe_page_url(page), 120),
            _truncate_for_log(str(e), 400),
        )
        raise BrowserResourceInvalidError(
            str(e),
            helper_name="stream_raw_via_context_request",
            operation="http_client",
            stage="request",
            resource_hint="transport",
            request_url=url,
            page_url=_safe_page_url(page),
            request_id=request_id,
            stream_phase="fetch",
        ) from e


async def _request_via_context_request(
    context: BrowserContext,
    page: Page | None,
    url: str,
    *,
    method: str = "GET",
    body: str | None = None,
    headers: dict[str, str] | None = None,
    multipart: dict[str, Any] | None = None,
    timeout_ms: int = 15000,
    request_id: str | None = None,
    helper_name: str,
) -> dict[str, Any]:
    logger.info(
        "[fetch] helper=%s method=%s request_id=%s url=%s page.url=%s",
        helper_name,
        method,
        request_id,
        _truncate_for_log(url, 120),
        _truncate_for_log(_safe_page_url(page), 120),
    )
    response = None
    try:
        response = await context.request.fetch(
            url,
            method=method,
            headers=headers or None,
            data=body,
            multipart=multipart,
            timeout=timeout_ms,
            fail_on_status_code=False,
        )
        text = await response.text()
        return {
            "ok": bool(response.ok),
            "status": int(response.status),
            "statusText": str(response.status_text),
            "url": str(response.url),
            "redirected": str(response.url) != url,
            "headers": {str(k): str(v) for k, v in response.headers.items()},
            "text": text,
        }
    except Exception as e:
        classified = _classify_browser_resource_error(
            e,
            helper_name=helper_name,
            operation="context.request",
            stage="fetch",
            request_url=url,
            page=page,
            request_id=request_id,
        )
        if classified is not None:
            raise classified from e
        logger.warning(
            "[fetch] helper=%s request_id=%s context.request failed url=%s page.url=%s err=%s",
            helper_name,
            request_id,
            _truncate_for_log(url, 120),
            _truncate_for_log(_safe_page_url(page), 120),
            _truncate_for_log(str(e), 400),
        )
        raise RuntimeError(str(e)) from e
    finally:
        if response is not None:
            try:
                await response.dispose()
            except Exception:
                pass


async def request_json_via_context_request(
    context: BrowserContext,
    page: Page | None,
    url: str,
    *,
    method: str = "GET",
    body: str | None = None,
    headers: dict[str, str] | None = None,
    timeout_ms: int = 15000,
    request_id: str | None = None,
) -> dict[str, Any]:
    result = await _request_via_context_request(
        context,
        page,
        url,
        method=method,
        body=body,
        headers=headers,
        timeout_ms=timeout_ms,
        request_id=request_id,
        helper_name="request_json_via_context_request",
    )
    return _attach_json_body(result, invalid_message="控制请求返回结果异常")


async def request_json_via_page_fetch(
    page: Page,
    url: str,
    *,
    method: str = "GET",
    body: str | None = None,
    headers: dict[str, str] | None = None,
    timeout_ms: int = 15000,
    request_id: str | None = None,
) -> dict[str, Any]:
    """
    在页面内发起非流式 fetch，请求结果按 JSON 优先解析返回。
    这样能复用浏览器真实网络栈、cookie 与代理扩展能力。
    """
    logger.info(
        "[fetch] helper=request_json_via_page_fetch method=%s request_id=%s url=%s page.url=%s",
        method,
        request_id,
        _truncate_for_log(url, 120),
        _truncate_for_log(_safe_page_url(page), 120),
    )
    try:
        result = await asyncio.wait_for(
            page.evaluate(
                PAGE_FETCH_JSON_JS,
                {
                    "url": url,
                    "method": method,
                    "body": body,
                    "headers": headers or {},
                    "timeoutMs": timeout_ms,
                },
            ),
            timeout=_evaluate_timeout_seconds(timeout_ms),
        )
    except asyncio.TimeoutError as e:
        logger.warning(
            "[fetch] helper=request_json_via_page_fetch request_id=%s evaluate timeout url=%s page.url=%s",
            request_id,
            _truncate_for_log(url, 120),
            _truncate_for_log(_safe_page_url(page), 120),
        )
        raise BrowserResourceInvalidError(
            f"page.evaluate timeout after {_evaluate_timeout_seconds(timeout_ms):.1f}s",
            helper_name="request_json_via_page_fetch",
            operation="page.evaluate",
            stage="evaluate_timeout",
            resource_hint="page",
            request_url=url,
            page_url=_safe_page_url(page),
            request_id=request_id,
        ) from e
    except Exception as e:
        classified = _classify_browser_resource_error(
            e,
            helper_name="request_json_via_page_fetch",
            operation="page.evaluate",
            stage="evaluate",
            request_url=url,
            page=page,
            request_id=request_id,
        )
        if classified is not None:
            raise classified from e
        raise
    return _attach_json_body(result, invalid_message="页面 fetch 返回结果异常")


async def upload_file_via_context_request(
    context: BrowserContext,
    page: Page | None,
    url: str,
    *,
    filename: str,
    mime_type: str,
    data: bytes,
    field_name: str = "file",
    extra_fields: dict[str, str] | None = None,
    timeout_ms: int = 30000,
    request_id: str | None = None,
) -> dict[str, Any]:
    multipart: dict[str, Any] = dict(extra_fields or {})
    multipart[field_name] = {
        "name": filename,
        "mimeType": mime_type or "application/octet-stream",
        "buffer": data,
    }
    result = await _request_via_context_request(
        context,
        page,
        url,
        method="POST",
        multipart=multipart,
        timeout_ms=timeout_ms,
        request_id=request_id,
        helper_name="upload_file_via_context_request",
    )
    return _attach_json_body(result, invalid_message="控制上传返回结果异常")


async def upload_file_via_page_fetch(
    page: Page,
    url: str,
    *,
    filename: str,
    mime_type: str,
    data: bytes,
    field_name: str = "file",
    extra_fields: dict[str, str] | None = None,
    timeout_ms: int = 30000,
    request_id: str | None = None,
) -> dict[str, Any]:
    logger.info(
        "[fetch] helper=upload_file_via_page_fetch filename=%s mime=%s request_id=%s url=%s page.url=%s",
        filename,
        mime_type,
        request_id,
        _truncate_for_log(url, 120),
        _truncate_for_log(_safe_page_url(page), 120),
    )
    try:
        result = await asyncio.wait_for(
            page.evaluate(
                PAGE_FETCH_MULTIPART_JS,
                {
                    "url": url,
                    "filename": filename,
                    "mimeType": mime_type,
                    "dataBase64": base64.b64encode(data).decode("ascii"),
                    "fieldName": field_name,
                    "extraFields": extra_fields or {},
                    "timeoutMs": timeout_ms,
                },
            ),
            timeout=_evaluate_timeout_seconds(timeout_ms),
        )
    except asyncio.TimeoutError as e:
        logger.warning(
            "[fetch] helper=upload_file_via_page_fetch request_id=%s evaluate timeout url=%s page.url=%s",
            request_id,
            _truncate_for_log(url, 120),
            _truncate_for_log(_safe_page_url(page), 120),
        )
        raise BrowserResourceInvalidError(
            f"page.evaluate timeout after {_evaluate_timeout_seconds(timeout_ms):.1f}s",
            helper_name="upload_file_via_page_fetch",
            operation="page.evaluate",
            stage="evaluate_timeout",
            resource_hint="page",
            request_url=url,
            page_url=_safe_page_url(page),
            request_id=request_id,
        ) from e
    except Exception as e:
        classified = _classify_browser_resource_error(
            e,
            helper_name="upload_file_via_page_fetch",
            operation="page.evaluate",
            stage="evaluate",
            request_url=url,
            page=page,
            request_id=request_id,
        )
        if classified is not None:
            raise classified from e
        raise
    return _attach_json_body(result, invalid_message="页面上传返回结果异常")


async def stream_raw_via_context_request(
    context: BrowserContext,
    page: Page | None,
    url: str,
    body: str,
    request_id: str,
    *,
    on_http_error: Callable[[str, dict[str, str] | None], int | None] | None = None,
    on_headers: Callable[[dict[str, str]], None] | None = None,
    fetch_timeout: float = 90.0,
    body_timeout: float = 300.0,
    proxy_url: str | None = None,
    proxy_auth: tuple[str, str] | None = None,
) -> AsyncIterator[str]:
    """通过真实流式 HTTP client 发起 completion 请求，避免先读完整 body。"""
    del fetch_timeout
    async for chunk in _stream_via_http_client(
        context,
        page,
        url,
        body,
        request_id,
        on_http_error=on_http_error,
        on_headers=on_headers,
        read_timeout=body_timeout,
        proxy_url=proxy_url,
        proxy_auth=proxy_auth,
    ):
        yield chunk


async def stream_raw_via_page_fetch(
    context: BrowserContext,
    page: Page,
    url: str,
    body: str,
    request_id: str,
    *,
    on_http_error: Callable[[str, dict[str, str] | None], int | None] | None = None,
    on_headers: Callable[[dict[str, str]], None] | None = None,
    error_state: dict[str, bool] | None = None,
    fetch_timeout: int = 90,
    read_timeout: float = 60.0,
) -> AsyncIterator[str]:
    """
    在浏览器内对 url 发起 POST body，流式回传原始字符串块（含 SSE 等）。
    同一 page 多请求用 request_id 区分 binding，互不串数据。
    通过 CDP Runtime.addBinding 注入 sendChunk_<request_id>，用 Runtime.bindingCalled 接收。
    收到 __headers__: 时解析 JSON 并调用 on_headers(headers)；收到 __error__: 时调用 on_http_error(msg)；收到 __done__ 结束。
    """
    chunk_queue: asyncio.Queue[str] = asyncio.Queue()
    binding_name = "sendChunk_" + request_id
    stream_phase = "cdp_setup"

    def on_binding_called(event: dict[str, Any]) -> None:
        name = event.get("name")
        payload = event.get("payload", "")
        if name == binding_name:
            chunk_queue.put_nowait(
                payload if isinstance(payload, str) else str(payload)
            )

    def classify_stream_error(
        exc: Exception,
        *,
        stage: str,
    ) -> BrowserResourceInvalidError | None:
        return _classify_browser_resource_error(
            exc,
            helper_name="stream_raw_via_page_fetch",
            operation="stream",
            stage=stage,
            request_url=url,
            page=page,
            request_id=request_id,
            stream_phase=stream_phase,
        )

    cdp = None
    fetch_task: asyncio.Task[None] | None = None
    try:
        try:
            cdp = await context.new_cdp_session(page)
        except Exception as e:
            classified = classify_stream_error(e, stage="new_cdp_session")
            if classified is not None:
                raise classified from e
            raise
        cdp.on("Runtime.bindingCalled", on_binding_called)
        try:
            await cdp.send("Runtime.addBinding", {"name": binding_name})
        except Exception as e:
            classified = classify_stream_error(e, stage="add_binding")
            if classified is not None:
                raise classified from e
            raise

        logger.info(
            "[fetch] helper=stream_raw_via_page_fetch request_id=%s stage=page.evaluate url=%s page.url=%s",
            request_id,
            _truncate_for_log(url, 120),
            _truncate_for_log(_safe_page_url(page), 120),
        )

        async def run_fetch() -> None:
            nonlocal stream_phase
            try:
                stream_phase = "page_evaluate"
                await asyncio.wait_for(
                    page.evaluate(
                        PAGE_FETCH_STREAM_JS,
                        {
                            "url": url,
                            "body": body,
                            "bindingName": binding_name,
                            "timeoutMs": max(1, int(fetch_timeout * 1000)),
                        },
                    ),
                    timeout=max(float(fetch_timeout) + 5.0, 10.0),
                )
            except asyncio.TimeoutError as e:
                logger.warning(
                    "[fetch] helper=stream_raw_via_page_fetch request_id=%s stage=page.evaluate evaluate timeout url=%s page.url=%s",
                    request_id,
                    _truncate_for_log(url, 120),
                    _truncate_for_log(_safe_page_url(page), 120),
                )
                raise BrowserResourceInvalidError(
                    f"page.evaluate timeout after {max(float(fetch_timeout) + 5.0, 10.0):.1f}s",
                    helper_name="stream_raw_via_page_fetch",
                    operation="stream",
                    stage="evaluate_timeout",
                    resource_hint="page",
                    request_url=url,
                    page_url=_safe_page_url(page),
                    request_id=request_id,
                    stream_phase=stream_phase,
                ) from e
            except Exception as e:
                classified = classify_stream_error(e, stage="page.evaluate")
                if classified is not None:
                    raise classified from e
                raise

        fetch_task = asyncio.create_task(run_fetch())
        try:
            headers = None
            while True:
                if fetch_task.done():
                    exc = fetch_task.exception()
                    if exc is not None:
                        raise exc
                try:
                    chunk = await asyncio.wait_for(
                        chunk_queue.get(), timeout=read_timeout
                    )
                except asyncio.TimeoutError as e:
                    stream_phase = "body"
                    logger.warning(
                        "[fetch] helper=stream_raw_via_page_fetch request_id=%s stream_phase=%s read timeout url=%s page.url=%s",
                        request_id,
                        stream_phase,
                        _truncate_for_log(url, 120),
                        _truncate_for_log(_safe_page_url(page), 120),
                    )
                    raise BrowserResourceInvalidError(
                        f"stream read timeout after {read_timeout:.1f}s",
                        helper_name="stream_raw_via_page_fetch",
                        operation="stream",
                        stage="read_timeout",
                        resource_hint="page",
                        request_url=url,
                        page_url=_safe_page_url(page),
                        request_id=request_id,
                        stream_phase=stream_phase,
                    ) from e
                if chunk == "__done__":
                    break
                if chunk.startswith("__headers__:"):
                    stream_phase = "headers"
                    try:
                        headers = json.loads(chunk[12:])
                        if on_headers and isinstance(headers, dict):
                            on_headers({k: str(v) for k, v in headers.items()})
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.debug("[fetch] 解析 __headers__ 失败: %s", e)
                    continue
                if chunk.startswith("__error__:"):
                    msg = chunk[10:].strip()
                    saw_terminal = bool(error_state and error_state.get("terminal"))
                    stream_phase = "terminal_event" if saw_terminal else ("body" if headers else "before_headers")
                    if on_http_error:
                        unfreeze_at = on_http_error(msg, headers)
                        if isinstance(unfreeze_at, int):
                            logger.warning("[fetch] __error__ from page: %s", msg)
                            raise AccountFrozenError(msg, unfreeze_at)
                    classified = _classify_browser_resource_error(
                        RuntimeError(msg),
                        helper_name="stream_raw_via_page_fetch",
                        operation="page_fetch_stream",
                        stage="page_error_event",
                        request_url=url,
                        page=page,
                        request_id=request_id,
                        stream_phase=stream_phase,
                    )
                    if classified is not None:
                        raise classified
                    if saw_terminal:
                        logger.info(
                            "[fetch] page fetch disconnected after terminal event request_id=%s stream_phase=%s: %s",
                            request_id,
                            stream_phase,
                            msg,
                        )
                        continue
                    logger.warning(
                        "[fetch] __error__ from page before terminal event request_id=%s stream_phase=%s: %s",
                        request_id,
                        stream_phase,
                        msg,
                    )
                    raise RuntimeError(msg)
                stream_phase = "body"
                yield chunk
        finally:
            if fetch_task is not None:
                done, pending = await asyncio.wait({fetch_task}, timeout=5.0)
                if pending:
                    fetch_task.cancel()
                    fetch_task.add_done_callback(_consume_background_task_result)
                else:
                    try:
                        fetch_task.result()
                    except asyncio.CancelledError:
                        pass
                    except BrowserResourceInvalidError:
                        pass
    finally:
        if cdp is not None:
            try:
                await asyncio.wait_for(cdp.detach(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[fetch] helper=stream_raw_via_page_fetch request_id=%s detach CDP session timeout page.url=%s",
                    request_id,
                    _truncate_for_log(_safe_page_url(page), 120),
                )
            except Exception as e:
                logger.debug("detach CDP session 时异常: %s", e)


def parse_sse_to_events(buffer: str, chunk: str) -> tuple[str, list[str]]:
    """
    把 chunk 追加到 buffer，按行拆出 data: 后的 payload 列表，返回 (剩余 buffer, payload 列表)。
    接入方对每个 payload 自行 JSON 解析并抽取 text / message_id / error。
    """
    buffer += chunk
    lines = buffer.split("\n")
    buffer = lines[-1]
    payloads: list[str] = []
    for line in lines[:-1]:
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]" or not payload:
            continue
        payloads.append(payload)
    return (buffer, payloads)


async def stream_completion_via_sse(
    context: BrowserContext,
    page: Page,
    url: str,
    body: str,
    parse_event: ParseSseEvent,
    request_id: str,
    *,
    on_http_error: Callable,
    is_terminal_event: Callable[[str], bool] | None = None,
    collect_message_id: list[str] | None = None,
    first_token_timeout: float = 15.0,
    transport: str = "page_fetch",
    transport_options: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """
    在浏览器内 POST 拿到流，按 SSE 行拆成 data 事件，用 parse_event(payload) 解析每条；
    逐块 yield 文本，可选把 message_id 收集到 collect_message_id。
    parse_event(payload) 返回 (texts, message_id, error)，error 非空时仅打 debug 日志不抛错。
    """
    buffer = ""
    stream_state: dict[str, bool] = {"terminal": False}
    saw_text = False
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    opts = dict(transport_options or {})
    if transport == "context_request":
        raw_stream = stream_raw_via_context_request(
            context,
            page,
            url,
            body,
            request_id,
            on_http_error=on_http_error,
            **opts,
        )
        resource_hint = "transport"
    else:
        raw_stream = stream_raw_via_page_fetch(
            context,
            page,
            url,
            body,
            request_id,
            on_http_error=on_http_error,
            error_state=stream_state,
        )
        resource_hint = "page"
    async for chunk in raw_stream:
        buffer, payloads = parse_sse_to_events(buffer, chunk)
        for payload in payloads:
            if is_terminal_event and is_terminal_event(payload):
                stream_state["terminal"] = True
            try:
                texts, message_id, error = parse_event(payload)
            except Exception as e:
                logger.debug("parse_stream_event 单条解析异常: %s", e)
                continue
            if error:
                logger.warning("SSE error from upstream: %s", error)
                raise RuntimeError(error)
            if message_id and collect_message_id is not None:
                collect_message_id.append(message_id)
            for t in texts:
                saw_text = True
                yield t
        if (
            not saw_text
            and not stream_state["terminal"]
            and loop.time() - started_at >= first_token_timeout
        ):
            raise BrowserResourceInvalidError(
                f"no text token received within {first_token_timeout:.1f}s",
                helper_name="stream_completion_via_sse",
                operation="parse_stream",
                stage="first_token_timeout",
                resource_hint=resource_hint,
                request_url=url,
                page_url=_safe_page_url(page),
                request_id=request_id,
                stream_phase="before_first_text",
            )

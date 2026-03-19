"""
OpenAI 协议路由。

支持：
- /openai/{provider}/v1/chat/completions
- /openai/{provider}/v1/models
- 旧路径 /{provider}/v1/...（等价于 OpenAI 协议）
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.api.auth import require_api_key
from core.api.chat_handler import ChatHandler
from core.plugin.base import PluginRegistry
from core.protocol.openai import OpenAIProtocolAdapter
from core.protocol.schemas import CanonicalChatRequest
from core.protocol.service import CanonicalChatService


def get_chat_handler(request: Request) -> ChatHandler:
    """从 app state 取出 ChatHandler。"""
    handler = getattr(request.app.state, "chat_handler", None)
    if handler is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    return handler


def resolve_request_model(
    provider: str,
    canonical_req: CanonicalChatRequest,
) -> CanonicalChatRequest:
    resolved = PluginRegistry.resolve_model(provider, canonical_req.model)
    canonical_req.model = resolved.public_model
    canonical_req.metadata["upstream_model"] = resolved.upstream_model
    return canonical_req


def create_router() -> APIRouter:
    """创建 OpenAI 协议路由。"""
    router = APIRouter(dependencies=[Depends(require_api_key)])
    adapter = OpenAIProtocolAdapter()

    def _list_models(provider: str) -> dict[str, Any]:
        try:
            metadata = PluginRegistry.model_metadata(provider)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {
                    "id": mid,
                    "object": "model",
                    "created": now,
                    "owned_by": provider,
                }
                for mid in metadata["public_models"]
            ],
        }

    @router.get("/openai/{provider}/v1/models")
    def list_models(provider: str) -> dict[str, Any]:
        return _list_models(provider)

    @router.get("/{provider}/v1/models")
    def list_models_legacy(provider: str) -> dict[str, Any]:
        return _list_models(provider)

    async def _chat_completions(
        provider: str,
        request: Request,
        handler: ChatHandler,
    ) -> Any:
        raw_body = await request.json()
        try:
            canonical_req = resolve_request_model(
                provider,
                adapter.parse_request(provider, raw_body),
            )
        except Exception as exc:
            status, payload = adapter.render_error(exc)
            return JSONResponse(status_code=status, content=payload)

        service = CanonicalChatService(handler)
        if canonical_req.stream:

            async def sse_stream() -> AsyncIterator[str]:
                try:
                    async for event in adapter.render_stream(
                        canonical_req,
                        service.stream_raw(canonical_req),
                    ):
                        yield event
                except Exception as exc:
                    status, payload = adapter.render_error(exc)
                    del status
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            return StreamingResponse(
                sse_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        try:
            raw_events = await service.collect_raw(canonical_req)
            return adapter.render_non_stream(canonical_req, raw_events)
        except Exception as exc:
            status, payload = adapter.render_error(exc)
            return JSONResponse(status_code=status, content=payload)

    @router.post("/openai/{provider}/v1/chat/completions")
    async def chat_completions(
        provider: str,
        request: Request,
        handler: ChatHandler = Depends(get_chat_handler),
    ) -> Any:
        return await _chat_completions(provider, request, handler)

    @router.post("/{provider}/v1/chat/completions")
    async def chat_completions_legacy(
        provider: str,
        request: Request,
        handler: ChatHandler = Depends(get_chat_handler),
    ) -> Any:
        return await _chat_completions(provider, request, handler)

    return router

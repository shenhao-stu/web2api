"""Anthropic 协议路由。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.api.auth import require_api_key
from core.api.chat_handler import ChatHandler
from core.api.routes import get_chat_handler, resolve_request_model, check_pro_model_access
from core.protocol.anthropic import AnthropicProtocolAdapter
from core.protocol.service import CanonicalChatService


def create_anthropic_router() -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_api_key)])
    adapter = AnthropicProtocolAdapter()

    @router.post("/anthropic/{provider}/v1/messages")
    async def messages(
        provider: str,
        request: Request,
        handler: ChatHandler = Depends(get_chat_handler),
    ) -> Any:
        return await _messages(provider, request, handler)

    @router.post("/{provider}/v1/messages")
    async def messages_legacy(
        provider: str,
        request: Request,
        handler: ChatHandler = Depends(get_chat_handler),
    ) -> Any:
        return await _messages(provider, request, handler)

    async def _messages(
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

        pro_err = check_pro_model_access(request, provider, canonical_req.model)
        if pro_err is not None:
            return pro_err

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
                    yield (
                        "event: error\n"
                        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    )

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

    return router

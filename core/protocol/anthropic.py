"""Anthropic 协议适配器。"""

from __future__ import annotations

import json
import time
import uuid as uuid_mod
from collections.abc import AsyncIterator
from typing import Any

from core.api.conv_parser import (
    decode_latest_session_id,
    extract_session_id_marker,
    strip_session_id_suffix,
)
from core.api.react import format_react_final_answer_content, parse_react_output
from core.api.react_stream_parser import ReactStreamParser
from core.hub.schemas import OpenAIStreamEvent
from core.protocol.base import ProtocolAdapter
from core.protocol.schemas import (
    CanonicalChatRequest,
    CanonicalContentBlock,
    CanonicalMessage,
    CanonicalToolSpec,
)


class AnthropicProtocolAdapter(ProtocolAdapter):
    protocol_name = "anthropic"

    def parse_request(
        self,
        provider: str,
        raw_body: dict[str, Any],
    ) -> CanonicalChatRequest:
        messages = raw_body.get("messages") or []
        if not isinstance(messages, list):
            raise ValueError("messages 必须为数组")
        system_blocks = self._parse_content(raw_body.get("system"))
        canonical_messages: list[CanonicalMessage] = []
        resume_session_id: str | None = None
        for item in messages:
            if not isinstance(item, dict):
                continue
            blocks = self._parse_content(item.get("content"))
            for block in blocks:
                text = block.text or ""
                decoded = decode_latest_session_id(text)
                if decoded:
                    resume_session_id = decoded
                    block.text = strip_session_id_suffix(text)
            canonical_messages.append(
                CanonicalMessage(
                    role=str(item.get("role") or "user"),
                    content=blocks,
                )
            )

        for block in system_blocks:
            text = block.text or ""
            decoded = decode_latest_session_id(text)
            if decoded:
                resume_session_id = decoded
                block.text = strip_session_id_suffix(text)

        tools = [self._parse_tool(tool) for tool in list(raw_body.get("tools") or [])]
        stop_sequences = raw_body.get("stop_sequences") or []
        return CanonicalChatRequest(
            protocol="anthropic",
            provider=provider,
            model=str(raw_body.get("model") or ""),
            system=system_blocks,
            messages=canonical_messages,
            stream=bool(raw_body.get("stream") or False),
            max_tokens=raw_body.get("max_tokens"),
            temperature=raw_body.get("temperature"),
            top_p=raw_body.get("top_p"),
            stop_sequences=[str(v) for v in stop_sequences if isinstance(v, str)],
            tools=tools,
            tool_choice=raw_body.get("tool_choice"),
            resume_session_id=resume_session_id,
        )

    def render_non_stream(
        self,
        req: CanonicalChatRequest,
        raw_events: list[OpenAIStreamEvent],
    ) -> dict[str, Any]:
        full = "".join(
            ev.content or ""
            for ev in raw_events
            if ev.type == "content_delta" and ev.content
        )
        session_marker = extract_session_id_marker(full)
        text = strip_session_id_suffix(full)
        message_id = self._message_id(req)
        if req.tools:
            parsed = parse_react_output(text)
            if parsed and parsed.get("type") == "tool_call":
                content: list[dict[str, Any]] = [
                    {
                        "type": "tool_use",
                        "id": f"toolu_{uuid_mod.uuid4().hex[:24]}",
                        "name": str(parsed.get("tool") or ""),
                        "input": parsed.get("params") or {},
                    }
                ]
                if session_marker:
                    content.append({"type": "text", "text": session_marker})
                return self._message_response(
                    req,
                    message_id,
                    content,
                    stop_reason="tool_use",
                )
            rendered = format_react_final_answer_content(text)
        else:
            rendered = text
        if session_marker:
            rendered += session_marker
        return self._message_response(
            req,
            message_id,
            [{"type": "text", "text": rendered}],
            stop_reason="end_turn",
        )

    async def render_stream(
        self,
        req: CanonicalChatRequest,
        raw_stream: AsyncIterator[OpenAIStreamEvent],
    ) -> AsyncIterator[str]:
        message_id = self._message_id(req)
        parser = ReactStreamParser(
            chat_id=f"chatcmpl-{uuid_mod.uuid4().hex[:24]}",
            model=req.model,
            created=int(time.time()),
            has_tools=bool(req.tools),
        )
        session_marker = ""
        translator = _AnthropicStreamTranslator(req, message_id)
        async for event in raw_stream:
            if event.type == "content_delta" and event.content:
                chunk = event.content
                if extract_session_id_marker(chunk) and not strip_session_id_suffix(
                    chunk
                ):
                    session_marker = chunk
                    continue
                for sse in parser.feed(chunk):
                    for out in translator.feed_openai_sse(sse):
                        yield out
            elif event.type == "finish":
                break
        for sse in parser.finish():
            for out in translator.feed_openai_sse(sse, session_marker=session_marker):
                yield out

    def render_error(self, exc: Exception) -> tuple[int, dict[str, Any]]:
        status = 400 if isinstance(exc, ValueError) else 500
        err_type = "invalid_request_error" if status == 400 else "api_error"
        return (
            status,
            {
                "type": "error",
                "error": {"type": err_type, "message": str(exc)},
            },
        )

    @staticmethod
    def _parse_tool(tool: dict[str, Any]) -> CanonicalToolSpec:
        return CanonicalToolSpec(
            name=str(tool.get("name") or ""),
            description=str(tool.get("description") or ""),
            input_schema=tool.get("input_schema") or {},
        )

    @staticmethod
    def _parse_content(value: Any) -> list[CanonicalContentBlock]:
        if value is None:
            return []
        if isinstance(value, str):
            return [CanonicalContentBlock(type="text", text=value)]
        if isinstance(value, list):
            blocks: list[CanonicalContentBlock] = []
            for item in value:
                if isinstance(item, str):
                    blocks.append(CanonicalContentBlock(type="text", text=item))
                    continue
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                if item_type == "text":
                    blocks.append(
                        CanonicalContentBlock(
                            type="text", text=str(item.get("text") or "")
                        )
                    )
                elif item_type == "image":
                    source = item.get("source") or {}
                    source_type = source.get("type")
                    if source_type == "base64":
                        blocks.append(
                            CanonicalContentBlock(
                                type="image",
                                mime_type=str(source.get("media_type") or ""),
                                data=str(source.get("data") or ""),
                            )
                        )
                elif item_type == "tool_result":
                    text_parts = AnthropicProtocolAdapter._parse_content(
                        item.get("content")
                    )
                    blocks.append(
                        CanonicalContentBlock(
                            type="tool_result",
                            tool_use_id=str(item.get("tool_use_id") or ""),
                            text="\n".join(
                                part.text or ""
                                for part in text_parts
                                if part.type == "text"
                            ),
                            is_error=bool(item.get("is_error") or False),
                        )
                    )
            return blocks
        raise ValueError("content 格式不合法")

    @staticmethod
    def _message_response(
        req: CanonicalChatRequest,
        message_id: str,
        content: list[dict[str, Any]],
        *,
        stop_reason: str,
    ) -> dict[str, Any]:
        return {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": req.model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    @staticmethod
    def _message_id(req: CanonicalChatRequest) -> str:
        return str(
            req.metadata.setdefault(
                "anthropic_message_id", f"msg_{uuid_mod.uuid4().hex}"
            )
        )


class _AnthropicStreamTranslator:
    def __init__(self, req: CanonicalChatRequest, message_id: str) -> None:
        self._req = req
        self._message_id = message_id
        self._started = False
        self._current_block_type: str | None = None
        self._current_index = -1
        self._pending_tool_id: str | None = None
        self._pending_tool_name: str | None = None
        self._stopped = False

    def feed_openai_sse(
        self,
        sse: str,
        *,
        session_marker: str = "",
    ) -> list[str]:
        lines = [line for line in sse.splitlines() if line.startswith("data: ")]
        out: list[str] = []
        for line in lines:
            payload = line[6:].strip()
            if payload == "[DONE]":
                continue
            obj = json.loads(payload)
            choice = (obj.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            finish_reason = choice.get("finish_reason")
            if not self._started:
                out.append(
                    self._event(
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {
                                "id": self._message_id,
                                "type": "message",
                                "role": "assistant",
                                "model": self._req.model,
                                "content": [],
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {"input_tokens": 0, "output_tokens": 0},
                            },
                        },
                    )
                )
                self._started = True

            content = delta.get("content")
            if isinstance(content, str) and content:
                out.extend(self._ensure_text_block())
                out.append(
                    self._event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": self._current_index,
                            "delta": {"type": "text_delta", "text": content},
                        },
                    )
                )

            tool_calls = delta.get("tool_calls") or []
            if tool_calls:
                head = tool_calls[0]
                if head.get("id") and head.get("function", {}).get("name") is not None:
                    out.extend(self._close_current_block())
                    self._current_index += 1
                    self._current_block_type = "tool_use"
                    self._pending_tool_id = str(head.get("id") or "")
                    self._pending_tool_name = str(
                        head.get("function", {}).get("name") or ""
                    )
                    out.append(
                        self._event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": self._current_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": self._pending_tool_id,
                                    "name": self._pending_tool_name,
                                    "input": {},
                                },
                            },
                        )
                    )
                args_delta = head.get("function", {}).get("arguments")
                if args_delta:
                    out.append(
                        self._event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": self._current_index,
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": str(args_delta),
                                },
                            },
                        )
                    )

            if finish_reason:
                if session_marker:
                    if finish_reason == "tool_calls":
                        out.extend(self._close_current_block())
                        out.extend(self._emit_marker_text_block(session_marker))
                    else:
                        out.extend(self._ensure_text_block())
                        out.append(
                            self._event(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": self._current_index,
                                    "delta": {
                                        "type": "text_delta",
                                        "text": session_marker,
                                    },
                                },
                            )
                        )
                out.extend(self._close_current_block())
                stop_reason = (
                    "tool_use" if finish_reason == "tool_calls" else "end_turn"
                )
                out.append(
                    self._event(
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {
                                "stop_reason": stop_reason,
                                "stop_sequence": None,
                            },
                            "usage": {"output_tokens": 0},
                        },
                    )
                )
                out.append(self._event("message_stop", {"type": "message_stop"}))
                self._stopped = True
        return out

    def _ensure_text_block(self) -> list[str]:
        if self._current_block_type == "text":
            return []
        out = self._close_current_block()
        self._current_index += 1
        self._current_block_type = "text"
        out.append(
            self._event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self._current_index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        )
        return out

    def _emit_marker_text_block(self, marker: str) -> list[str]:
        self._current_index += 1
        self._current_block_type = "text"
        return [
            self._event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self._current_index,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            self._event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._current_index,
                    "delta": {"type": "text_delta", "text": marker},
                },
            ),
            self._event(
                "content_block_stop",
                {"type": "content_block_stop", "index": self._current_index},
            ),
        ]

    def _close_current_block(self) -> list[str]:
        if self._current_block_type is None:
            return []
        block_index = self._current_index
        self._current_block_type = None
        return [
            self._event(
                "content_block_stop",
                {"type": "content_block_stop", "index": block_index},
            )
        ]

    @staticmethod
    def _event(event_name: str, payload: dict[str, Any]) -> str:
        del event_name
        return f"event: {payload['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

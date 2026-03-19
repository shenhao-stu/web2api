"""OpenAI 协议适配器。"""

from __future__ import annotations

import json
import re
import time
import uuid as uuid_mod
from collections.abc import AsyncIterator
from typing import Any

from core.api.conv_parser import (
    extract_session_id_marker,
    parse_conv_uuid_from_messages,
    strip_session_id_suffix,
)
from core.api.function_call import build_tool_calls_response
from core.api.react import (
    format_react_final_answer_content,
    parse_react_output,
    react_output_to_tool_calls,
)
from core.api.react_stream_parser import ReactStreamParser
from core.api.schemas import OpenAIChatRequest, OpenAIContentPart, OpenAIMessage
from core.hub.schemas import OpenAIStreamEvent
from core.protocol.base import ProtocolAdapter
from core.protocol.schemas import (
    CanonicalChatRequest,
    CanonicalContentBlock,
    CanonicalMessage,
    CanonicalToolSpec,
)


class OpenAIProtocolAdapter(ProtocolAdapter):
    protocol_name = "openai"

    def parse_request(
        self,
        provider: str,
        raw_body: dict[str, Any],
    ) -> CanonicalChatRequest:
        req = OpenAIChatRequest.model_validate(raw_body)
        resume_session_id = parse_conv_uuid_from_messages(
            [self._message_to_raw_dict(m) for m in req.messages]
        )
        system_blocks: list[CanonicalContentBlock] = []
        messages: list[CanonicalMessage] = []
        for msg in req.messages:
            blocks = self._to_blocks(msg.content)
            if msg.role == "system":
                system_blocks.extend(blocks)
            else:
                messages.append(CanonicalMessage(role=msg.role, content=blocks))
        tools = [self._to_tool_spec(tool) for tool in list(req.tools or [])]
        return CanonicalChatRequest(
            protocol="openai",
            provider=provider,
            model=req.model,
            system=system_blocks,
            messages=messages,
            stream=req.stream,
            tools=tools,
            tool_choice=req.tool_choice,
            resume_session_id=resume_session_id,
        )

    def render_non_stream(
        self,
        req: CanonicalChatRequest,
        raw_events: list[OpenAIStreamEvent],
    ) -> dict[str, Any]:
        reply = "".join(
            ev.content or ""
            for ev in raw_events
            if ev.type == "content_delta" and ev.content
        )
        session_marker = extract_session_id_marker(reply)
        content_for_parse = strip_session_id_suffix(reply)
        chat_id, created = self._response_context(req)
        if req.tools:
            parsed = parse_react_output(content_for_parse)
            tool_calls_list = react_output_to_tool_calls(parsed) if parsed else []
            if tool_calls_list:
                thought_ns = ""
                if "Thought" in content_for_parse:
                    match = re.search(
                        r"Thought[:：]\s*(.+?)(?=\s*Action[:：]|$)",
                        content_for_parse,
                        re.DOTALL | re.I,
                    )
                    thought_ns = (match.group(1) or "").strip() if match else ""
                text_content = (
                    f"<think>{thought_ns}</think>\n{session_marker}".strip()
                    if thought_ns
                    else session_marker
                )
                return build_tool_calls_response(
                    tool_calls_list,
                    chat_id,
                    req.model,
                    created,
                    text_content=text_content,
                )
            content_reply = format_react_final_answer_content(content_for_parse)
            if session_marker:
                content_reply += session_marker
        else:
            content_reply = content_for_parse
        return {
            "id": chat_id,
            "object": "chat.completion",
            "created": created,
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content_reply},
                    "finish_reason": "stop",
                }
            ],
        }

    async def render_stream(
        self,
        req: CanonicalChatRequest,
        raw_stream: AsyncIterator[OpenAIStreamEvent],
    ) -> AsyncIterator[str]:
        chat_id, created = self._response_context(req)
        parser = ReactStreamParser(
            chat_id=chat_id,
            model=req.model,
            created=created,
            has_tools=bool(req.tools),
        )
        session_marker = ""
        async for event in raw_stream:
            if event.type == "content_delta" and event.content:
                chunk = event.content
                if extract_session_id_marker(chunk) and not strip_session_id_suffix(
                    chunk
                ):
                    session_marker = chunk
                    continue
                for sse in parser.feed(chunk):
                    yield sse
            elif event.type == "finish":
                break
        if session_marker:
            yield self._content_delta(chat_id, req.model, created, session_marker)
        for sse in parser.finish():
            yield sse

    def render_error(self, exc: Exception) -> tuple[int, dict[str, Any]]:
        status = 400 if isinstance(exc, ValueError) else 500
        err_type = "invalid_request_error" if status == 400 else "server_error"
        return (
            status,
            {"error": {"message": str(exc), "type": err_type}},
        )

    @staticmethod
    def _message_to_raw_dict(msg: OpenAIMessage) -> dict[str, Any]:
        if isinstance(msg.content, list):
            content: str | list[dict[str, Any]] = [p.model_dump() for p in msg.content]
        else:
            content = msg.content
        out: dict[str, Any] = {"role": msg.role, "content": content}
        if msg.tool_calls is not None:
            out["tool_calls"] = msg.tool_calls
        if msg.tool_call_id is not None:
            out["tool_call_id"] = msg.tool_call_id
        return out

    @staticmethod
    def _to_blocks(
        content: str | list[OpenAIContentPart] | None,
    ) -> list[CanonicalContentBlock]:
        if content is None:
            return []
        if isinstance(content, str):
            return [
                CanonicalContentBlock(
                    type="text", text=strip_session_id_suffix(content)
                )
            ]
        blocks: list[CanonicalContentBlock] = []
        for part in content:
            if part.type == "text":
                blocks.append(
                    CanonicalContentBlock(
                        type="text",
                        text=strip_session_id_suffix(part.text or ""),
                    )
                )
            elif part.type == "image_url":
                image_url = part.image_url
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                if not url:
                    continue
                if isinstance(url, str) and url.startswith("data:"):
                    blocks.append(CanonicalContentBlock(type="image", data=url))
                else:
                    blocks.append(CanonicalContentBlock(type="image", url=str(url)))
        return blocks

    @staticmethod
    def _to_tool_spec(tool: dict[str, Any]) -> CanonicalToolSpec:
        function = tool.get("function") if tool.get("type") == "function" else tool
        return CanonicalToolSpec(
            name=str(function.get("name") or ""),
            description=str(function.get("description") or ""),
            input_schema=function.get("parameters")
            or function.get("input_schema")
            or {},
            strict=bool(function.get("strict") or False),
        )

    @staticmethod
    def _content_delta(chat_id: str, model: str, created: int, text: str) -> str:
        return (
            "data: "
            + json.dumps(
                {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": text},
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )

    @staticmethod
    def _response_context(req: CanonicalChatRequest) -> tuple[str, int]:
        chat_id = str(
            req.metadata.setdefault(
                "response_id", f"chatcmpl-{uuid_mod.uuid4().hex[:24]}"
            )
        )
        created = int(req.metadata.setdefault("created", int(time.time())))
        return chat_id, created
